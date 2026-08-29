import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from docx import Document
from modules.atlas_ai import (
    AtlasAIConfig,
    AtlasAIError,
    _answer_text,
    _atlas_corpus_abbreviations,
    _atlas_pinpoint_labels,
    _atlas_query_variants,
    _atlas_task_profile,
    _bounded_dialog_messages,
    _cross_chat_context,
    _chunks,
    _compact_overlay_answer,
    _recent_user_dialog_context,
    atlas_ai_config,
    atlas_answer,
    atlas_answer_stream,
    atlas_embed,
    atlas_ensure_collection,
    atlas_index_source,
    atlas_parse_text_mode,
    atlas_probe_collection,
    atlas_research_plan,
    atlas_search,
)
from modules.atlas_agents import atlas_agent_catalog, atlas_resolve_agent
from modules.atlas_knowledge import AtlasKnowledgeFileError, atlas_extract_knowledge_file
from modules.atlas_taxonomy import atlas_classify_knowledge
from modules.atlas_forum_sync import AtlasForumScrapeBatch, AtlasForumSnapshot
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import atlas_repository
from persistence.core import connect


class AtlasRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_dashboard_is_idempotent_and_seeds_one_template_version(self) -> None:
        first = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        second = atlas_repository.atlas_dashboard(77, 42, "Пользователь")

        self.assertEqual(first["organization"]["id"], second["organization"]["id"])
        self.assertEqual(first["membership"]["role"], "owner")
        self.assertEqual(len(second["templates"]), 3)
        with connect() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM atlas_document_templates").fetchone()[0],
                3,
            )

    def test_overlay_character_binding_is_owned_scoped_and_uses_dynamic_catalog(self) -> None:
        character = storage.add_profile_character(77, 42, "Saul Goodman", "263345")
        storage.add_profile_character(77, 99, "Other Person", "777777")

        context = atlas_repository.atlas_set_overlay_character(
            77,
            42,
            character.id,
            server_code="phoenix-15",
            faction_code="fib",
            rank="Special Agent",
            screen_context_enabled=True,
        )

        self.assertTrue(context["selected_character"]["identity_verified"])
        self.assertEqual(context["selected_character"]["faction_code"], "fib")
        self.assertEqual(context["selected_character"]["rank"], "Special Agent")
        self.assertTrue(context["selected_character"]["screen_context_enabled"])
        preserved = atlas_repository.atlas_set_overlay_character(
            77,
            42,
            character.id,
            server_code="phoenix-15",
            faction_code="gov",
        )
        self.assertEqual(preserved["selected_character"]["rank"], "Special Agent")
        self.assertEqual(len(context["characters"]), 1)
        self.assertEqual(
            {item["code"] for item in context["catalog"]["factions"]},
            {"lspd", "lscsd", "fib", "gov", "sang", "ems", "wn"},
        )
        with self.assertRaisesRegex(ValueError, "atlas_overlay_character_not_owned"):
            atlas_repository.atlas_set_overlay_character(
                77,
                99,
                character.id,
                server_code="phoenix-15",
                faction_code="gov",
            )

    def test_overlay_binding_table_is_added_to_existing_database_without_data_loss(self) -> None:
        character = storage.add_profile_character(77, 42, "Saul Goodman", "263345")
        with connect() as con:
            con.execute("DROP TABLE atlas_character_bindings")
            con.commit()

        storage.init_db()

        with connect() as con:
            table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("atlas_character_bindings",),
            ).fetchone()
            preserved = con.execute(
                "SELECT nickname, static_id FROM profile_characters WHERE id = ?",
                (int(character.id),),
            ).fetchone()
        self.assertIsNotNone(table)
        self.assertEqual(tuple(preserved), ("Saul Goodman", "263345"))

    def test_answer_feedback_is_saved_and_bad_answer_is_excluded_from_memory(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        thread_id = atlas_repository.atlas_create_thread(organization_id, 42, "Проверка")
        atlas_repository.atlas_add_message(thread_id, "user", "Вопрос")
        message_id = atlas_repository.atlas_add_message(thread_id, "assistant", "Неточный ответ")

        feedback = atlas_repository.atlas_set_message_feedback(
            organization_id, 42, message_id, "bad"
        )
        thread = atlas_repository.atlas_thread_messages(organization_id, 42, thread_id)
        memory = atlas_repository.atlas_recent_chat_memory(organization_id, 42)

        self.assertEqual(feedback["rating"], "bad")
        self.assertEqual(thread["messages"][-1]["feedback_rating"], "bad")
        self.assertNotIn(message_id, {int(item["id"]) for item in memory})
        with self.assertRaisesRegex(ValueError, "atlas_feedback_message_not_found"):
            atlas_repository.atlas_set_message_feedback(
                organization_id, 99, message_id, "good"
            )

    def test_agent_threads_have_isolated_memory_lanes(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        general = atlas_repository.atlas_create_thread(
            organization_id, 42, "Общий", agent_id="atlas-tvr-a"
        )
        claims = atlas_repository.atlas_create_thread(
            organization_id, 42, "Иск", agent_id="atlas-claims"
        )
        atlas_repository.atlas_add_message(general, "user", "Личная общая заметка")
        atlas_repository.atlas_add_message(claims, "user", "Факты искового дела")

        memory = atlas_repository.atlas_recent_chat_memory(
            organization_id, 42, agent_id="atlas-claims"
        )
        stored = atlas_repository.atlas_thread_messages(organization_id, 42, claims)

        self.assertEqual(stored["thread"]["agent_id"], "atlas-claims")
        self.assertEqual([item["content_text"] for item in memory], ["Факты искового дела"])
        self.assertNotIn("Личная общая заметка", str(memory))

    def test_onboarding_and_document_are_persisted(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        membership = atlas_repository.atlas_update_onboarding(
            organization_id,
            42,
            step=4,
            profile={"agency": "SGL", "position": "Адвокат"},
        )
        report_template = next(
            item for item in dashboard["templates"] if item["code"] == "incident-report"
        )
        document = atlas_repository.atlas_create_document(
            organization_id,
            42,
            title="Рапорт №1",
            template_id=int(report_template["id"]),
            fields={
                "date": "2026-08-22",
                "location": "Phoenix",
                "participants": "Пользователь",
                "facts": "Проверено",
                "actions": "Материал сохранён",
            },
        )

        self.assertEqual(membership["onboarding_step"], 4)
        self.assertEqual(membership["profile"]["agency"], "SGL")
        self.assertEqual(document["title"], "Рапорт №1")
        timeline = atlas_repository.atlas_timeline_events(organization_id)
        self.assertEqual(timeline[0]["event_kind"], "document")
        self.assertEqual(timeline[0]["source_id"], str(document["id"]))
        snapshot = atlas_repository.atlas_admin_snapshot(77)
        self.assertEqual(snapshot["totals"]["documents"], 1)
        self.assertEqual(snapshot["recent_events"][0]["event_type"], "document_created")
        with self.assertRaisesRegex(ValueError, "atlas_document_template_forbidden"):
            atlas_repository.atlas_create_document(
                organization_id,
                42,
                title="Документ с чужим шаблоном",
                template_id=999999,
                fields={},
            )

    def test_continuity_timeline_is_idempotent_and_space_isolated(self) -> None:
        own = atlas_repository.atlas_dashboard(77, 42, "Первый")
        foreign = atlas_repository.atlas_dashboard(88, 84, "Второй")
        organization_id = int(own["organization"]["id"])
        first = atlas_repository.atlas_create_timeline_event(
            organization_id,
            42,
            title="Конфликт у штаба",
            summary="Сохранён исходный контекст события.",
            event_kind="incident",
            importance="important",
            dedupe_key="incident:headquarters:1",
        )
        repeated = atlas_repository.atlas_create_timeline_event(
            organization_id,
            42,
            title="Повторная отправка",
            dedupe_key="incident:headquarters:1",
        )

        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(atlas_repository.atlas_timeline_summary(organization_id)["attention"], 1)
        self.assertEqual(len(atlas_repository.atlas_timeline_events(organization_id)), 1)
        self.assertEqual(
            atlas_repository.atlas_timeline_events(int(foreign["organization"]["id"])),
            [],
        )
        with self.assertRaisesRegex(ValueError, "atlas_timeline_forbidden"):
            atlas_repository.atlas_create_timeline_event(
                organization_id,
                999,
                title="Чужое событие",
            )

    def test_continuity_graph_links_existing_entities_without_duplicates(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        event = atlas_repository.atlas_create_timeline_event(
            organization_id,
            42,
            title="Материал получен",
            event_kind="activity",
        )
        document = atlas_repository.atlas_create_document(
            organization_id,
            42,
            title="Сводка события",
            template_id=None,
            fields={},
        )
        first = atlas_repository.atlas_link_entities(
            organization_id,
            42,
            source_type="timeline_event",
            source_id=event["id"],
            relation="produced",
            target_type="document",
            target_id=document["id"],
        )
        repeated = atlas_repository.atlas_link_entities(
            organization_id,
            42,
            source_type="timeline_event",
            source_id=event["id"],
            relation="produced",
            target_type="document",
            target_id=document["id"],
        )

        self.assertEqual(first["id"], repeated["id"])
        links = atlas_repository.atlas_entity_links(
            organization_id, "document", document["id"]
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["source_id"], str(event["id"]))

        with self.assertRaisesRegex(ValueError, "atlas_entity_target_not_found"):
            atlas_repository.atlas_link_entities(
                organization_id,
                42,
                source_type="timeline_event",
                source_id=event["id"],
                relation="produced",
                target_type="document",
                target_id=999999,
            )
        with self.assertRaisesRegex(ValueError, "atlas_entity_type_invalid"):
            atlas_repository.atlas_link_entities(
                organization_id,
                42,
                source_type="invented",
                source_id=event["id"],
                relation="related_to",
                target_type="document",
                target_id=document["id"],
            )

    def test_timeline_update_uses_versions_and_page_cursor(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        created = [
            atlas_repository.atlas_create_timeline_event(
                organization_id,
                42,
                title=f"Событие {index}",
                occurred_at="2026-08-22T12:00:00+00:00",
            )
            for index in range(3)
        ]

        first_page = atlas_repository.atlas_timeline_page(organization_id, limit=2)
        second_page = atlas_repository.atlas_timeline_page(
            organization_id,
            limit=2,
            cursor=first_page["next_cursor"],
        )
        self.assertEqual([item["id"] for item in first_page["items"]], [created[2]["id"], created[1]["id"]])
        self.assertEqual([item["id"] for item in second_page["items"]], [created[0]["id"]])
        self.assertIsNone(second_page["next_cursor"])

        resolved = atlas_repository.atlas_update_timeline_event(
            organization_id,
            42,
            int(created[0]["id"]),
            status="resolved",
            summary="Результат сохранён.",
            expected_version=1,
        )
        self.assertEqual(resolved["version"], 2)
        self.assertEqual(resolved["status"], "resolved")
        self.assertIsNotNone(resolved["resolved_at"])
        with self.assertRaisesRegex(ValueError, "atlas_timeline_version_conflict"):
            atlas_repository.atlas_update_timeline_event(
                organization_id,
                42,
                int(created[0]["id"]),
                status="active",
                expected_version=1,
            )

        foreign = atlas_repository.atlas_dashboard(88, 84, "Чужой")
        with self.assertRaisesRegex(ValueError, "atlas_timeline_source_not_found"):
            atlas_repository.atlas_create_timeline_event(
                organization_id,
                42,
                title="Чужой источник",
                source_type="document",
                source_id=atlas_repository.atlas_create_document(
                    int(foreign["organization"]["id"]),
                    84,
                    title="Чужой документ",
                    template_id=None,
                    fields={},
                )["id"],
            )

    def test_admin_snapshot_does_not_mix_guilds(self) -> None:
        own = atlas_repository.atlas_dashboard(77, 42, "Первый")
        foreign = atlas_repository.atlas_dashboard(88, 84, "Второй")
        atlas_repository.atlas_create_document(
            int(own["organization"]["id"]), 42, title="Свой", template_id=None, fields={}
        )
        atlas_repository.atlas_create_document(
            int(foreign["organization"]["id"]), 84, title="Чужой", template_id=None, fields={}
        )

        snapshot = atlas_repository.atlas_admin_snapshot(77)
        self.assertEqual(snapshot["totals"]["organizations"], 1)
        self.assertEqual(snapshot["totals"]["documents"], 1)
        self.assertEqual({item["summary"] for item in snapshot["recent_events"]}, {"Создан документ «Свой»"})

    def test_knowledge_requires_editor_membership_and_deduplicates(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        first = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="Регламент",
            content="Проверенный текст внутреннего регламента длиной больше двадцати символов.",
            source_kind="regulation",
        )
        second = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="Регламент — новая подпись",
            content="Проверенный текст внутреннего регламента длиной больше двадцати символов.",
            source_kind="regulation",
        )

        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "atlas_knowledge_forbidden"):
            atlas_repository.atlas_add_knowledge(
                organization_id,
                999,
                title="Чужой источник",
                content="Этот пользователь не состоит в выбранной организации Atlas.",
            )
        rebuild_sources = atlas_repository.atlas_indexable_knowledge_sources()
        self.assertEqual([item["id"] for item in rebuild_sources], [first["id"]])
        self.assertEqual(rebuild_sources[0]["content_text"], first["content_text"])
        timeline = atlas_repository.atlas_timeline_events(organization_id)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["source_type"], "knowledge_source")
        self.assertEqual(timeline[0]["source_id"], str(first["id"]))

    def test_searchable_corpus_keeps_old_reference_documents_ahead_of_recent_noise(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        law = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="Уголовный кодекс штата San Andreas",
            content="Старая, но действующая нормативная база с полным текстом статей.",
            visibility_scope="server",
        )
        for index in range(4):
            atlas_repository.atlas_add_knowledge(
                organization_id,
                42,
                title=f"Новость форума {index}",
                content=f"Свежий информационный материал номер {index}, не являющийся кодексом.",
                visibility_scope="server",
            )

        sources = atlas_repository.atlas_searchable_knowledge_sources(
            organization_id,
            server_code="phoenix-15",
            faction_code="lspd",
            query_terms=("уголов",),
            limit=2,
        )

        self.assertEqual(sources[0]["id"], law["id"])
        self.assertEqual(sources[0]["title"], "Уголовный кодекс штата San Andreas")

    def test_knowledge_is_separated_by_server_and_faction(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        text = "Один и тот же общий текст может действовать в разных государственных структурах."
        lspd = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="LSPD",
            content=text,
            server_code="phoenix-15",
            faction_code="lspd",
        )
        gov = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="GOV",
            content=text,
            server_code="phoenix-15",
            faction_code="gov",
        )

        self.assertNotEqual(lspd["id"], gov["id"])
        self.assertEqual(
            [item["id"] for item in atlas_repository.atlas_knowledge_sources(
                organization_id, server_code="phoenix-15", faction_code="gov"
            )],
            [gov["id"]],
        )
        with self.assertRaisesRegex(ValueError, "atlas_faction_invalid"):
            atlas_repository.atlas_normalize_scope("phoenix-15", "unknown")

    def test_knowledge_visibility_combines_shared_and_isolates_private_data(self) -> None:
        owner = atlas_repository.atlas_dashboard(77, 42, "Владелец")
        guest = atlas_repository.atlas_dashboard(77, 84, "Другая организация")
        owner_id = int(owner["organization"]["id"])
        guest_id = int(guest["organization"]["id"])

        created = {}
        for scope, faction in (
            ("global", "lspd"),
            ("server", "lspd"),
            ("faction", "lspd"),
            ("workspace", "lspd"),
        ):
            created[scope] = atlas_repository.atlas_add_knowledge(
                owner_id,
                42,
                title=scope,
                content=f"Проверенный материал области {scope}, достаточно длинный для Atlas.",
                server_code="phoenix-15",
                faction_code=faction,
                visibility_scope=scope,
            )

        gov_ids = {
            int(item["id"])
            for item in atlas_repository.atlas_knowledge_sources(
                guest_id,
                server_code="phoenix-15",
                faction_code="gov",
            )
        }
        self.assertEqual(
            gov_ids,
            {int(created["global"]["id"]), int(created["server"]["id"])},
        )

        guest_lspd_ids = {
            int(item["id"])
            for item in atlas_repository.atlas_knowledge_sources(
                guest_id,
                server_code="phoenix-15",
                faction_code="lspd",
            )
        }
        self.assertIn(int(created["faction"]["id"]), guest_lspd_ids)
        self.assertNotIn(int(created["workspace"]["id"]), guest_lspd_ids)

        owner_lspd_ids = {
            int(item["id"])
            for item in atlas_repository.atlas_knowledge_sources(
                owner_id,
                server_code="phoenix-15",
                faction_code="lspd",
            )
        }
        self.assertEqual(owner_lspd_ids, {int(item["id"]) for item in created.values()})
        with self.assertRaisesRegex(ValueError, "atlas_knowledge_scope_invalid"):
            atlas_repository.atlas_add_knowledge(
                owner_id,
                42,
                title="Ошибка",
                content="Материал с неизвестной областью доступа не должен сохраниться.",
                visibility_scope="unknown",
            )

    def test_admin_catalog_and_private_space_are_dynamic_and_isolated(self) -> None:
        server = atlas_repository.atlas_upsert_server(
            42,
            code="legacy-16",
            name="Legacy",
            number=16,
        )
        faction = atlas_repository.atlas_upsert_faction(
            42,
            code="ems",
            name="Emergency Medical Services",
            short_name="EMS",
        )
        private_space = atlas_repository.atlas_create_organization(
            77,
            42,
            name="Медицинский контур",
            owner_user_id=84,
            server_code=server["code"],
            faction_code=faction["code"],
        )
        selected = atlas_repository.atlas_dashboard(
            77,
            84,
            "Врач",
            organization_id=int(private_space["id"]),
        )
        outsider = atlas_repository.atlas_dashboard(77, 99, "Посторонний")

        self.assertEqual(selected["organization"]["id"], private_space["id"])
        self.assertEqual(selected["organization"]["branding"]["faction_code"], "ems")
        self.assertIn("legacy-16", {item["code"] for item in selected["catalog"]["servers"]})
        self.assertIn("ems", {item["code"] for item in selected["catalog"]["factions"]})
        self.assertNotIn(
            int(private_space["id"]),
            {int(item["id"]) for item in outsider["spaces"]},
        )

    def test_knowledge_taxonomy_is_saved_with_source(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Редактор")
        source = atlas_repository.atlas_add_knowledge(
            int(dashboard["organization"]["id"]),
            42,
            title="Правила сервера Phoenix",
            content=(
                "OOC правила сервера устанавливают требования администрации и наказания "
                "за нарушения игрового процесса."
            ),
            source_kind="url",
        )

        self.assertEqual(source["metadata"]["taxonomy"]["domain"], "ooc")
        self.assertEqual(source["metadata"]["taxonomy"]["corpus_kind"], "server_rule")

    def test_taxonomy_migration_preserves_canonical_source_and_rebuilds_only_index(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Редактор")
        source = atlas_repository.atlas_add_knowledge(
            int(dashboard["organization"]["id"]),
            42,
            title="Сохранённый кодекс",
            content="Полная сохранённая редакция кодекса длиной больше двадцати символов.",
        )
        atlas_repository.atlas_mark_knowledge_indexed(int(source["id"]), point_id="old-point")
        with connect() as con:
            con.execute(
                "UPDATE atlas_knowledge_sources SET metadata_json = '{}' WHERE id = ?",
                (int(source["id"]),),
            )
            con.execute("DELETE FROM meta WHERE key = ?", ("migration:atlas-taxonomy:2026-08-09-v1",))
            con.commit()

        storage.init_db()

        with connect() as con:
            stored = con.execute(
                "SELECT * FROM atlas_knowledge_sources WHERE id = ?",
                (int(source["id"]),),
            ).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["content_text"], source["content_text"])
        self.assertEqual(stored["status"], "pending")
        self.assertIsNone(stored["qdrant_point_id"])
        visible = atlas_repository.atlas_knowledge_sources(
            int(dashboard["organization"]["id"])
        )[0]
        self.assertEqual(visible["metadata"]["taxonomy"]["corpus_kind"], "law")

    def test_chat_history_is_ordered_and_private_between_users(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        first = atlas_repository.atlas_create_thread(organization_id, 42, "Первый чат")
        second = atlas_repository.atlas_create_thread(organization_id, 42, "Второй чат")
        foreign = atlas_repository.atlas_create_thread(organization_id, 999, "Чужой чат")
        atlas_repository.atlas_add_message(first, "user", "Подготовь речь о реформе")
        atlas_repository.atlas_add_message(first, "assistant", "Начнём с правовой основы.")
        atlas_repository.atlas_add_message(second, "user", "Предпочитаю спокойный официальный стиль")
        atlas_repository.atlas_add_message(foreign, "user", "Секрет другого пользователя")

        threads = atlas_repository.atlas_threads(organization_id, 42)
        first_history = atlas_repository.atlas_thread_messages(
            organization_id,
            42,
            first,
        )
        memory = atlas_repository.atlas_recent_chat_memory(
            organization_id,
            42,
            exclude_thread_id=first,
        )

        self.assertEqual({item["id"] for item in threads}, {first, second})
        self.assertEqual(
            [item["role"] for item in first_history["messages"]],
            ["user", "assistant"],
        )
        self.assertEqual([item["thread_title"] for item in memory], ["Второй чат"])
        self.assertNotIn("Секрет другого пользователя", str(memory))
        with self.assertRaisesRegex(ValueError, "atlas_thread_not_found"):
            atlas_repository.atlas_thread_messages(organization_id, 42, foreign)

    def test_discord_thread_binding_and_atlas_audit_are_durable(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        thread_id = atlas_repository.atlas_create_thread(organization_id, 42, "Задержание")
        bound = atlas_repository.atlas_bind_discord_thread(
            discord_thread_id=9001,
            guild_id=77,
            parent_channel_id=1494602485485277194,
            organization_id=organization_id,
            atlas_thread_id=thread_id,
            owner_user_id=42,
        )
        atlas_repository.atlas_record_event(
            organization_id,
            42,
            "ai_answer_created",
            "Atlas ответил в Discord",
            target_type="discord_thread",
            target_id=9001,
        )

        self.assertEqual(bound["atlas_thread_id"], thread_id)
        self.assertEqual(atlas_repository.atlas_discord_thread(9001)["owner_user_id"], 42)
        self.assertEqual(
            atlas_repository.atlas_admin_snapshot(77)["recent_events"][0]["target_id"],
            "9001",
        )


class AtlasAITests(unittest.IsolatedAsyncioTestCase):
    def test_answer_text_accepts_provider_content_variants(self) -> None:
        self.assertEqual(
            _answer_text(
                {
                    "choices": [{
                        "message": {
                            "content": [
                                {"type": "output_text", "text": {"value": "Готовый "}},
                                {"type": "text", "text": "ответ"},
                            ]
                        }
                    }]
                }
            ),
            "Готовый ответ",
        )
        self.assertEqual(
            _answer_text(
                {"choices": [{"message": {"content": None}, "text": "Резервный ответ"}]}
            ),
            "Резервный ответ",
        )

    def test_agent_registry_preserves_general_and_adds_specialists(self) -> None:
        catalog = atlas_agent_catalog()
        self.assertEqual(catalog[0]["id"], "atlas-tvr-a")
        self.assertIn("atlas-claims", {item["id"] for item in catalog})
        self.assertIn("исков", atlas_resolve_agent("atlas-claims").specialty.casefold())
        with self.assertRaisesRegex(ValueError, "atlas_agent_invalid"):
            atlas_resolve_agent("unknown")

    def test_expensive_legacy_default_is_downgraded_to_economy_model(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_OPENROUTER_MODEL": "openai/gpt-5.4"},
        ):
            self.assertEqual(atlas_ai_config().chat_model, "openai/gpt-5-mini")

    def test_weak_legacy_default_is_upgraded_to_economy_model(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_OPENROUTER_MODEL": "openai/gpt-4.1-mini"},
        ):
            self.assertEqual(atlas_ai_config().chat_model, "openai/gpt-5-mini")

    def test_custom_atlas_model_is_preserved(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_OPENROUTER_MODEL": "custom/provider-model"},
        ):
            self.assertEqual(atlas_ai_config().chat_model, "custom/provider-model")

    def test_deprecated_direct_model_is_upgraded_to_grok_43(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_DIRECT_MODEL": "x-ai/grok-4.1-fast"},
        ):
            self.assertEqual(atlas_ai_config().direct_model, "x-ai/grok-4.3")

    def test_atlas_2_is_text_only_and_strips_its_call_prefix(self) -> None:
        self.assertEqual(
            atlas_parse_text_mode("  Атлас 2, скажи прямо  "),
            ("скажи прямо", True),
        )
        self.assertEqual(
            atlas_parse_text_mode("Атлас 2, скажи прямо", latency_mode="overlay"),
            ("Атлас 2, скажи прямо", False),
        )

    async def test_atlas_2_routes_to_direct_model_and_reports_mode(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat/completions",
            chat_model="test/standard",
            direct_model="x-ai/test-direct",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "Прямой ответ"}}]}
        )
        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai.atlas_search",
            AsyncMock(return_value=[]),
        ), patch("modules.atlas_ai._json_request", completion):
            result = await atlas_answer(77, "Атлас 2, скажи прямо")

        payload = completion.await_args.kwargs["payload"]
        self.assertEqual(payload["model"], "x-ai/test-direct")
        self.assertEqual(payload["messages"][-1]["content"], "скажи прямо")
        self.assertIn("без стилистической цензуры", payload["messages"][0]["content"])
        self.assertEqual(result["text_mode"], "atlas-2")

    def test_taxonomy_distinguishes_ic_ooc_charters_and_case_law(self) -> None:
        ooc = atlas_classify_knowledge(
            title="Правила сервера",
            content="OOC требования проекта и наказания администрации за нарушения.",
        )
        charter = atlas_classify_knowledge(
            title="Устав LSPD",
            content="Настоящий устав определяет полномочия сотрудников полиции и ранги.",
        )
        practice = atlas_classify_knowledge(
            title="Судебная практика",
            content="Решение суда по исковому заявлению и толкованию положений закона.",
        )

        self.assertEqual((ooc["domain"], ooc["corpus_kind"]), ("ooc", "server_rule"))
        self.assertEqual((charter["domain"], charter["corpus_kind"]), ("ic", "charter"))
        self.assertEqual(practice["corpus_kind"], "case_law")
        self.assertEqual(practice["authority_scope"], "court")

    def test_aristotle_plan_adapts_to_legal_case(self) -> None:
        plan = atlas_research_plan("Составь позицию по иску и судебной практике")
        self.assertEqual(plan[0]["agent"], "Навигатор")
        self.assertEqual(plan[-1]["agent"], "Аристотель")
        self.assertIn("practice", {item["id"] for item in plan})

    def test_chunker_is_bounded_and_preserves_overlap(self) -> None:
        chunks = _chunks("слово " * 4000, size=1000, overlap=100)
        self.assertGreater(len(chunks), 2)
        self.assertLessEqual(len(chunks), 48)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

    def test_query_variants_expand_legal_abbreviation(self) -> None:
        variants = _atlas_query_variants("Что такое УК?")

        self.assertTrue(any("уголовный кодекс" in item.casefold() for item in variants))

    def test_corpus_abbreviations_follow_actual_atlas_documents(self) -> None:
        aliases = _atlas_corpus_abbreviations(
            [
                {"title": "Процессуальный Кодекс штата San Andreas"},
                {"title": "Административный кодекс штата San Andreas"},
                {"title": "Кодекс этики и служебного поведения"},
            ]
        )

        self.assertEqual(aliases["пк"], "Процессуальный Кодекс штата San Andreas")
        self.assertEqual(aliases["ак"], "Административный кодекс штата San Andreas")
        self.assertEqual(aliases["кэ"], "Кодекс этики и служебного поведения")
        self.assertNotIn("упк", aliases)
        self.assertNotIn("коап", aliases)

    def test_bad_answer_is_not_reused_inside_current_dialog(self) -> None:
        messages = _bounded_dialog_messages(
            [
                {"role": "user", "content_text": "Вопрос"},
                {"role": "assistant", "content_text": "Ошибка", "feedback_rating": "bad"},
                {"role": "user", "content_text": "Попробуй ещё раз"},
            ]
        )

        self.assertEqual([item["content"] for item in messages], ["Вопрос", "Попробуй ещё раз"])

    def test_followup_search_uses_only_one_relevant_dialog_anchor(self) -> None:
        dialog = _bounded_dialog_messages(
            [
                {"role": "user", "content_text": "Сначала обсуждали форму рапорта"},
                {"role": "assistant", "content_text": "Вот форма"},
                {"role": "user", "content_text": "Теперь обсуждаем задержание по статье 16.1"},
                {"role": "assistant", "content_text": "Проверяю норму"},
            ]
        )

        profile = _atlas_task_profile(
            "А теперь объясни её простыми словами",
            mode="balanced",
            dialog_messages=dialog,
        )

        self.assertTrue(profile.is_followup)
        self.assertIn("задержание по статье 16.1", profile.retrieval_query)
        self.assertNotIn("форму рапорта", profile.retrieval_query)

    def test_fresh_question_does_not_pollute_search_with_old_dialog(self) -> None:
        profile = _atlas_task_profile(
            "Что такое Уголовный кодекс?",
            mode="balanced",
            dialog_messages=[
                {"role": "user", "content": "Составь речь про выборы"},
                {"role": "assistant", "content": "Готовая речь"},
            ],
        )

        self.assertFalse(profile.is_followup)
        self.assertEqual(profile.retrieval_query, "Что такое Уголовный кодекс?")
        self.assertNotIn("выборы", profile.retrieval_query)

        second = _atlas_task_profile(
            "А меня задержали, что делать?",
            mode="balanced",
            dialog_messages=[{"role": "user", "content": "Составь речь про выборы"}],
        )
        self.assertFalse(second.is_followup)
        self.assertNotIn("выборы", second.retrieval_query)

    def test_exact_chapter_request_is_not_treated_as_freeform_drafting(self) -> None:
        profile = _atlas_task_profile(
            "Напиши мне 16 главу УК полностью",
            mode="balanced",
        )

        self.assertEqual(profile.intent, "exact_lookup")
        self.assertEqual(profile.depth, "deep")

    def test_corpus_abbreviation_is_understood_by_task_router(self) -> None:
        profile = _atlas_task_profile("Что такое УК?", mode="balanced")

        self.assertEqual(profile.intent, "legal_analysis")
        self.assertEqual(profile.depth, "quick")

    def test_plain_greeting_stays_social_instead_of_describing_the_interface(self) -> None:
        profile = _atlas_task_profile("Привет!", mode="balanced")

        self.assertEqual(profile.intent, "social")
        self.assertEqual(profile.depth, "quick")
        self.assertIn("обычное человеческое обращение", profile.response_brief)

    def test_contextual_drafting_reuses_the_described_situation(self) -> None:
        profile = _atlas_task_profile(
            "На основе уже описанной ситуации составь жалобу",
            mode="balanced",
            dialog_messages=[
                {"role": "user", "content": "Меня задержали без представления сотрудника"},
                {"role": "assistant", "content": "Проверяю процедуру"},
            ],
        )

        self.assertTrue(profile.is_followup)
        self.assertEqual(profile.intent, "drafting")
        self.assertIn("задержали без представления", profile.retrieval_query)

    def test_research_context_keeps_two_latest_user_corrections(self) -> None:
        context = _recent_user_dialog_context(
            [
                {"role": "user", "content": "Старая не относящаяся тема"},
                {"role": "assistant", "content": "Старый ответ"},
                {"role": "user", "content": "Сотрудник не представился"},
                {"role": "assistant", "content": "Первичный анализ"},
                {"role": "user", "content": "Но ордер выдала прокуратура"},
            ]
        )

        self.assertNotIn("Старая", context)
        self.assertIn("не представился", context)
        self.assertIn("ордер выдала прокуратура", context)

    def test_pinpoint_labels_are_conservative_and_support_exact_references(self) -> None:
        self.assertEqual(
            _atlas_pinpoint_labels({"reference": "chapter:16", "text": "16.1 Текст"}),
            ["глава 16"],
        )
        self.assertEqual(
            _atlas_pinpoint_labels(
                {
                    "text": (
                        "2.6 Порядок начала задержания.\n"
                        "Описание нормы.\n"
                        "2.11 Право задержанного на защиту."
                    )
                }
            ),
            ["статья 2.6", "статья 2.11"],
        )

    def test_cross_chat_memory_does_not_reuse_unrated_model_claims(self) -> None:
        context = _cross_chat_context(
            [
                {
                    "role": "user",
                    "thread_title": "Предпочтения",
                    "content_text": "Пиши официально и кратко",
                },
                {
                    "role": "assistant",
                    "thread_title": "Старый ответ",
                    "content_text": "Статья якобы разрешает обыск",
                },
                {
                    "role": "assistant",
                    "feedback_rating": "good",
                    "thread_title": "Подтверждённый ответ",
                    "content_text": "Пользователь подтвердил этот удачный шаблон",
                },
            ]
        )

        self.assertIn("Пиши официально", context)
        self.assertNotIn("якобы разрешает", context)
        self.assertIn("удачный шаблон", context)

    def test_dialog_keeps_initial_user_brief_and_recent_turns(self) -> None:
        history = [{"role": "user", "content_text": "Главная цель: защитить клиента"}]
        for index in range(40):
            history.append(
                {
                    "role": "assistant" if index % 2 else "user",
                    "content_text": f"Сообщение {index}",
                }
            )

        messages = _bounded_dialog_messages(history, max_messages=8, max_chars=2000)

        self.assertEqual(len(messages), 8)
        self.assertEqual(messages[0]["content"], "Главная цель: защитить клиента")
        self.assertEqual(messages[-1]["content"], "Сообщение 39")

    async def test_hybrid_search_finds_saved_source_when_qdrant_returns_nothing(self) -> None:
        source = {
            "id": 91,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Уголовный кодекс",
            "content_text": (
                "Уголовный кодекс устанавливает основания ответственности, "
                "виды преступлений и применяемые наказания."
            ),
            "source_url": "https://forum.majestic-rp.ru/threads/uk.1/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            result = await atlas_search(77, "Что такое УК?", expanded=True)

        self.assertTrue(result)
        self.assertEqual(result[0]["source_id"], 91)
        self.assertEqual(result[0]["title"], "Уголовный кодекс")

    async def test_search_returns_explicitly_requested_legal_chapter(self) -> None:
        source = {
            "id": 93,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Уголовный Кодекс штата San Andreas",
            "content_text": (
                "Глава 15.\nПредыдущие нормы.\n"
                "Глава 16.\nПреступления против правосудия.\nСтатья 16.1. Точный текст.\n"
                "Глава 17.\nСледующие нормы."
            ),
            "source_url": "https://forum.majestic-rp.ru/threads/uk.3/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            result = await atlas_search(77, "Напиши мне 16 главу УК", expanded=True)

        self.assertTrue(result[0]["structured"])
        self.assertIn("Глава 16", result[0]["text"])
        self.assertIn("Статья 16.1", result[0]["text"])
        self.assertNotIn("Глава 17", result[0]["text"])

    async def test_search_returns_exact_article_instead_of_nearby_reference(self) -> None:
        source = {
            "id": 94,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Уголовный Кодекс штата San Andreas",
            "content_text": (
                "Глава 16.\nПреступления против правосудия.\n"
                "16.1\nПервая точная норма.\n"
                "16.1.2\nДругая вложенная норма.\n"
                "16.2\nСледующая статья."
            ),
            "source_url": "https://forum.majestic-rp.ru/threads/uk.4/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            result = await atlas_search(77, "Покажи статью 16.1 УК", expanded=True)

        self.assertEqual(result[0]["reference"], "article:16.1")
        self.assertIn("Первая точная норма", result[0]["text"])
        self.assertNotIn("Другая вложенная норма", result[0]["text"])

    async def test_exact_article_survives_forum_markup_and_nonbreaking_spaces(self) -> None:
        source = {
            "id": 95,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Уголовный Кодекс штата San Andreas",
            "content_text": (
                "# [B]Статья\u00a016.1[/B]\nТочная норма из оформленной темы.\n"
                "[SIZE=5][B]Статья 16.2[/B][/SIZE]\nСледующая норма."
            ),
            "source_url": "https://forum.majestic-rp.ru/threads/uk.5/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            result = await atlas_search(77, "Покажи статью 16.1 УК", expanded=True)

        self.assertEqual(result[0]["reference"], "article:16.1")
        self.assertIn("Точная норма из оформленной темы", result[0]["text"])
        self.assertNotIn("Следующая норма", result[0]["text"])

    async def test_arabic_chapter_request_matches_roman_forum_heading(self) -> None:
        source = {
            "id": 96,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Уголовный Кодекс штата San Andreas",
            "content_text": (
                "[CENTER][B]ГЛАВА XVI[/B][/CENTER]\nНужная глава.\n"
                "[CENTER][B]ГЛАВА XVII[/B][/CENTER]\nСледующая глава."
            ),
            "source_url": "https://forum.majestic-rp.ru/threads/uk.6/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            result = await atlas_search(77, "Напиши 16 главу УК", expanded=True)

        self.assertEqual(result[0]["reference"], "chapter:16")
        self.assertIn("Нужная глава", result[0]["text"])
        self.assertNotIn("Следующая глава", result[0]["text"])

    async def test_exact_chapter_survives_markdown_heading_decoration(self) -> None:
        source = {
            "id": 961,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Уголовный Кодекс штата San Andreas",
            "content_text": (
                "**ГЛАВА 16. ПРЕСТУПЛЕНИЯ ПРОТИВ ПРАВОСУДИЯ**\n"
                "Статья 16.1. Точная норма.\n"
                "**ГЛАВА 17. ИНЫЕ ПРЕСТУПЛЕНИЯ**\nСледующая глава."
            ),
            "source_url": "https://forum.majestic-rp.ru/threads/uk.61/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            result = await atlas_search(77, "Покажи главу 16 УК", expanded=True)

        self.assertEqual(result[0]["reference"], "chapter:16")
        self.assertIn("Точная норма", result[0]["text"])
        self.assertNotIn("Следующая глава", result[0]["text"])

    async def test_lexical_fallback_matches_russian_word_forms(self) -> None:
        source = {
            "id": 962,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Процессуальный кодекс",
            "content_text": "Порядок задержания требует разъяснить гражданину основание процедуры.",
            "source_url": "https://forum.majestic-rp.ru/threads/process.62/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(side_effect=AtlasAIError("upstream_unavailable", "offline", retryable=True)),
        ):
            result = await atlas_search(77, "Меня задержали, что делать?", expanded=True)

        self.assertEqual(result[0]["source_id"], 962)
        self.assertIn("задержания", result[0]["text"])

    async def test_hybrid_search_uses_saved_source_when_semantic_search_is_down(self) -> None:
        source = {
            "id": 92,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Уголовный кодекс",
            "content_text": "Уголовный кодекс определяет преступления и ответственность.",
            "source_url": "https://forum.majestic-rp.ru/threads/uk.2/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(
                side_effect=AtlasAIError(
                    "upstream_rate_limited",
                    "temporary limit",
                    retryable=True,
                )
            ),
        ):
            result = await atlas_search(77, "Что такое УК?", expanded=True)

        self.assertTrue(result)
        self.assertEqual(result[0]["source_id"], 92)

    async def test_search_uses_all_accessible_knowledge_scopes(self) -> None:
        response = {
            "result": {
                "points": [
                    {
                        "score": 0.91,
                        "payload": {"organization_id": 77, "source_id": 4, "title": "Регламент", "text": "Текст"},
                    }
                ]
            }
        }
        with patch("modules.atlas_ai.atlas_embed", AsyncMock(return_value=[[0.1, 0.2]])), patch(
            "modules.atlas_ai._json_request", AsyncMock(return_value=response)
        ) as request:
            result = await atlas_search(77, "полномочия")

        payload = request.await_args.kwargs["payload"]
        self.assertEqual(
            payload["filter"]["must"][0],
            {
                "key": "access_scope",
                "match": {
                    "any": [
                        "global",
                        "server:phoenix-15",
                        "faction:phoenix-15:lspd",
                        "workspace:77:phoenix-15:lspd",
                    ]
                },
            },
        )
        self.assertEqual(result[0]["source_id"], 4)

    async def test_search_applies_server_and_faction_filters(self) -> None:
        with patch("modules.atlas_ai.atlas_embed", AsyncMock(return_value=[[0.1, 0.2]])), patch(
            "modules.atlas_ai._json_request", AsyncMock(return_value={"result": {"points": []}})
        ) as request:
            await atlas_search(
                77,
                "порядок задержания",
                server_code="phoenix-15",
                faction_code="lspd",
            )

        scopes = request.await_args.kwargs["payload"]["filter"]["must"][0]
        self.assertEqual(scopes["key"], "access_scope")
        self.assertIn("server:phoenix-15", scopes["match"]["any"])
        self.assertIn("faction:phoenix-15:lspd", scopes["match"]["any"])
        self.assertIn("workspace:77:phoenix-15:lspd", scopes["match"]["any"])

    async def test_search_embeds_agent_queries_independently(self) -> None:
        embedded: list[str] = []

        async def embed(texts: list[str]) -> list[list[float]]:
            embedded.extend(texts)
            return [[0.1, 0.2] for _ in texts]

        with patch("modules.atlas_ai.atlas_embed", side_effect=embed), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            await atlas_search(
                77,
                "основной вопрос",
                query_variants=["процесс задержания", "исключения из правила"],
            )

        self.assertEqual(
            embedded,
            ["основной вопрос", "процесс задержания", "исключения из правила"],
        )

    async def test_expanded_search_does_not_drop_late_verification_queries(self) -> None:
        embedded: list[str] = []

        async def embed(texts: list[str]) -> list[list[float]]:
            embedded.extend(texts)
            return [[0.1, 0.2] for _ in texts]

        checks = [
            "основная норма",
            "исключение",
            "компетенция",
            "срок процедуры",
            "порядок обжалования",
        ]
        with patch("modules.atlas_ai.atlas_embed", side_effect=embed), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            await atlas_search(
                77,
                "законность задержания",
                expanded=True,
                query_variants=checks,
            )

        self.assertEqual(embedded[:6], ["законность задержания", *checks])

    async def test_planned_queries_also_work_in_lexical_fallback(self) -> None:
        source = {
            "id": 95,
            "organization_id": 1,
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "title": "Процессуальный кодекс",
            "content_text": "Специальное исключение разрешает прекратить процессуальное действие.",
            "source_url": "https://forum.majestic-rp.ru/threads/process.5/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(
                side_effect=AtlasAIError(
                    "upstream_unavailable",
                    "temporary outage",
                    retryable=True,
                )
            ),
        ):
            result = await atlas_search(
                77,
                "Что делать дальше?",
                query_variants=["специальное исключение процессуальное действие"],
            )

        self.assertEqual(result[0]["source_id"], 95)
        self.assertIn("исключение", result[0]["text"])

    async def test_index_payload_contains_one_canonical_access_scope(self) -> None:
        source = {
            "id": 5,
            "organization_id": 77,
            "server_code": "phoenix-15",
            "faction_code": "gov",
            "visibility_scope": "server",
            "title": "Общий регламент",
            "content_text": "Проверенный общий материал Phoenix длиной больше двадцати символов.",
        }
        embed = AsyncMock(return_value=[[0.1, 0.2]])
        with patch(
            "modules.atlas_ai.atlas_embed",
            embed,
        ), patch(
            "modules.atlas_ai.atlas_ensure_collection",
            AsyncMock(),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {}}),
        ) as request:
            await atlas_index_source(source)

        put_call = next(call for call in request.await_args_list if call.args[0] == "PUT")
        delete_call = next(call for call in request.await_args_list if call.args[0] == "POST")
        point = put_call.kwargs["payload"]["points"][0]
        self.assertIn("Название документа: Общий регламент", embed.await_args.args[0][0])
        self.assertEqual(point["payload"]["access_scope"], "server:phoenix-15")
        self.assertEqual(point["payload"]["visibility_scope"], "server")
        self.assertEqual(point["payload"]["knowledge_domain"], "mixed")
        self.assertEqual(point["payload"]["corpus_kind"], "procedure")
        self.assertEqual(point["payload"]["index_version"], 2)
        self.assertGreater(len(delete_call.kwargs["payload"]["points"]), 0)
        self.assertLess(
            request.await_args_list.index(put_call),
            request.await_args_list.index(delete_call),
        )

    async def test_collection_is_created_only_when_missing(self) -> None:
        missing = AtlasAIError("upstream_not_found", "missing")
        request = AsyncMock(side_effect=[missing, {"result": True}])
        with patch("modules.atlas_ai._json_request", request):
            await atlas_ensure_collection(1536)
        self.assertEqual(request.await_count, 2)
        self.assertEqual(request.await_args.kwargs["payload"]["vectors"]["size"], 1536)

        denied = AsyncMock(side_effect=AtlasAIError("upstream_error", "denied"))
        with patch("modules.atlas_ai._json_request", denied), self.assertRaises(AtlasAIError):
            await atlas_ensure_collection(1536)
        self.assertEqual(denied.await_count, 1)

    async def test_embedding_retries_temporary_rate_limit(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat/completions",
            chat_model="test/chat",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        request = AsyncMock(
            side_effect=[
                AtlasAIError("upstream_rate_limited", "limit", retryable=True),
                {"data": [{"embedding": [0.1, 0.2]}]},
            ]
        )
        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai._json_request", request
        ), patch("modules.atlas_ai.asyncio.sleep", AsyncMock()) as sleep:
            vectors = await atlas_embed(["Уголовный кодекс"])

        self.assertEqual(vectors, [[0.1, 0.2]])
        self.assertEqual(request.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_search_converts_qdrant_gridstore_panic_into_recovery_state(self) -> None:
        corrupted = AtlasAIError(
            "qdrant_index_corrupted",
            "Service internal error: task panicked with OutputTooSmall",
            retryable=True,
        )
        with patch(
            "modules.atlas_ai.atlas_embed",
            AsyncMock(return_value=[[0.1, 0.2]]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(side_effect=corrupted),
        ), self.assertRaises(AtlasAIError) as raised:
            await atlas_search(77, "порядок задержания")

        self.assertEqual(raised.exception.code, "atlas_index_recovery_required")
        self.assertTrue(raised.exception.retryable)
        self.assertIn("Материалы сохранены", str(raised.exception))

    async def test_collection_probe_detects_missing_and_corrupt_payload(self) -> None:
        missing = AsyncMock(side_effect=AtlasAIError("upstream_not_found", "missing"))
        with patch("modules.atlas_ai._json_request", missing):
            self.assertEqual((await atlas_probe_collection())["status"], "missing")

        corrupted = AsyncMock(
            side_effect=[
                {"result": {"points_count": 1}},
                AtlasAIError(
                    "qdrant_index_corrupted",
                    "Gridstore OutputTooSmall",
                    retryable=True,
                ),
            ]
        )
        with patch("modules.atlas_ai._json_request", corrupted):
            self.assertEqual((await atlas_probe_collection())["status"], "corrupted")

        stale = AsyncMock(
            side_effect=[
                {"result": {"points_count": 12}},
                {"result": {"points": [{"payload": {"title": "Старый индекс"}}]}},
            ]
        )
        with patch("modules.atlas_ai._json_request", stale):
            self.assertEqual((await atlas_probe_collection())["status"], "stale")

        current = AsyncMock(
            side_effect=[
                {"result": {"points_count": 12}},
                {"result": {"points": [{"payload": {"index_version": 2}}]}},
            ]
        )
        with patch("modules.atlas_ai._json_request", current):
            self.assertEqual((await atlas_probe_collection())["status"], "ok")

    async def test_openrouter_answer_is_delivered_as_real_sse_deltas(self) -> None:
        async def completion(request: web.Request) -> web.StreamResponse:
            self.assertTrue((await request.json())["stream"])
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write('data: {"choices":[{"delta":{"content":"Первый "}}]}\n\n'.encode())
            await response.write('data: {"choices":[{"delta":{"content":"фрагмент"}}]}\n\n'.encode())
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/chat", completion)
        server = TestServer(app)
        await server.start_server()
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url=str(server.make_url("/chat")),
            chat_model="test/model",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        chunks: list[str] = []

        async def receive(text: str) -> None:
            chunks.append(text)

        try:
            with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
                "modules.atlas_ai.atlas_search",
                AsyncMock(return_value=[]),
            ):
                result = await atlas_answer_stream(77, "Ответь по частям", on_delta=receive)
        finally:
            await server.close()

        self.assertEqual(chunks, ["Первый ", "фрагмент"])
        self.assertEqual(result["answer"], "Первый фрагмент")
        self.assertEqual(result["model"], "atlas-tvr-a")

    async def test_empty_stream_is_retried_once_as_visible_completion(self) -> None:
        requests: list[dict] = []

        async def completion(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            requests.append(body)
            if body.get("stream"):
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(
                    'data: {"choices":[{"delta":{"content":null,"reasoning":"скрыто"},"finish_reason":"length"}]}\n\n'.encode()
                )
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
                return response
            return web.json_response(
                {"choices": [{"message": {"content": "Ответ после безопасного повтора"}}]}
            )

        app = web.Application()
        app.router.add_post("/chat", completion)
        server = TestServer(app)
        await server.start_server()
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url=str(server.make_url("/chat")),
            chat_model="openai/gpt-5-mini",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        chunks: list[str] = []

        async def receive(text: str) -> None:
            chunks.append(text)

        try:
            with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
                "modules.atlas_ai.atlas_search", AsyncMock(return_value=[])
            ):
                result = await atlas_answer_stream(77, "Дай ответ", on_delta=receive)
        finally:
            await server.close()

        self.assertEqual(chunks, ["Ответ после безопасного повтора"])
        self.assertEqual(result["answer"], "Ответ после безопасного повтора")
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[0]["stream"])
        self.assertNotIn("stream", requests[1])
        self.assertEqual(requests[1]["reasoning"]["effort"], "minimal")
        self.assertGreaterEqual(requests[1]["max_tokens"], 3200)

    async def test_mid_stream_provider_error_is_not_reported_as_empty_answer(self) -> None:
        async def completion(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                'data: {"error":{"code":400,"message":"invalid request"},"choices":[{"delta":{"content":""},"finish_reason":"error"}]}\n\n'.encode()
            )
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/chat", completion)
        server = TestServer(app)
        await server.start_server()
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url=str(server.make_url("/chat")),
            chat_model="test/model",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )

        try:
            with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
                "modules.atlas_ai.atlas_search", AsyncMock(return_value=[])
            ):
                with self.assertRaises(AtlasAIError) as raised:
                    await atlas_answer_stream(77, "Дай ответ", on_delta=AsyncMock())
        finally:
            await server.close()

        self.assertEqual(raised.exception.code, "upstream_error")
        self.assertIn("invalid request", str(raised.exception))

    async def test_aristotle_stream_reports_visible_research_progress(self) -> None:
        calls: list[str] = []

        async def completion(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            if not body.get("stream"):
                system = str(body["messages"][0]["content"])
                if "архитектор исследовательской команды" in system:
                    calls.append("planner")
                    return web.json_response(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "content": json.dumps(
                                            {
                                                "mission": "Подготовить защиту при задержании",
                                                "agents": [
                                                    {
                                                        "agent": "Страж процедуры",
                                                        "role": "Процесс задержания",
                                                        "title": "Проверить действия сотрудника",
                                                        "task": "Сверить порядок задержания и права лица",
                                                        "search_query": "порядок задержания права задержанного",
                                                    },
                                                    {
                                                        "agent": "Контраргумент",
                                                        "role": "Риски позиции",
                                                        "title": "Проверить слабые места защиты",
                                                        "task": "Найти исключения и риски выбранной позиции",
                                                        "search_query": "исключения риски при задержании",
                                                    },
                                                ],
                                            },
                                            ensure_ascii=False,
                                        )
                                    }
                                }
                            ]
                        }
                    )
                calls.append("agent")
                return web.json_response(
                    {"choices": [{"message": {"content": "Проверка завершена [Источник 1]"}}]}
                )
            self.assertEqual(body["temperature"], 0.28)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write('data: {"choices":[{"delta":{"content":"Итог [1]"}}]}\n\n'.encode())
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/chat", completion)
        server = TestServer(app)
        await server.start_server()
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url=str(server.make_url("/chat")),
            chat_model="test/model",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        progress: list[dict] = []

        async def receive(_text: str) -> None:
            return None

        async def report(event: dict) -> None:
            progress.append(event)

        sources = [{
            "source_id": 4,
            "title": "Процессуальный кодекс",
            "text": "Проверенная норма",
            "knowledge_domain": "ic",
            "corpus_kind": "law",
            "score": 0.9,
            "url": None,
        }]
        try:
            with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
                "modules.atlas_ai.atlas_search",
                AsyncMock(return_value=sources),
            ) as search:
                result = await atlas_answer_stream(
                    77,
                    "Проведи полное исследование по задержанию",
                    response_mode="aristotle",
                    on_delta=receive,
                    on_progress=report,
                )
        finally:
            await server.close()

        self.assertEqual(result["response_mode"], "aristotle")
        self.assertTrue(result["research_plan"])
        self.assertEqual(
            [step["agent"] for step in result["research_plan"]],
            ["Страж процедуры", "Контраргумент", "Аристотель"],
        )
        self.assertEqual(calls, ["planner", "agent", "agent"])
        self.assertTrue(search.await_args.kwargs["expanded"])
        self.assertIn("plan", {event["phase"] for event in progress})
        self.assertIn("evidence", {event["phase"] for event in progress})
        self.assertEqual(
            sum(event["phase"] == "agent_result" for event in progress),
            2,
        )
        self.assertEqual(progress[-1]["phase"], "complete")

    async def test_creative_answer_uses_current_history_cross_chat_memory_and_sources(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat",
            chat_model="openai/gpt-5-mini",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        sources = [
            {
                "source_id": 7,
                "title": "Закон",
                "url": "https://example.test/law",
                "text": "Публичная речь должна соблюдать требования закона.",
                "score": 0.91,
            }
        ]
        response = {"choices": [{"message": {"content": "Готовая убедительная речь [1]"}}]}
        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai.atlas_search",
            AsyncMock(return_value=sources),
        ) as search, patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value=response),
        ) as request:
            result = await atlas_answer(
                77,
                "Теперь составь полную речь",
                history=[
                    {"role": "user", "content_text": "Мы обсуждаем судебную реформу"},
                    {"role": "assistant", "content_text": "Правовую основу я нашёл"},
                ],
                memory=[
                    {
                        "role": "user",
                        "thread_title": "Стиль выступления",
                        "content_text": "Предпочитаю спокойный официальный тон",
                    }
                ],
                response_mode="creative",
            )

        payload = request.await_args.kwargs["payload"]
        messages = payload["messages"]
        self.assertEqual(payload["temperature"], 0.68)
        self.assertEqual(payload["reasoning"]["effort"], "medium")
        self.assertIn("судебную реформу", search.await_args.args[1])
        self.assertTrue(any("Предпочитаю спокойный" in item["content"] for item in messages))
        self.assertTrue(any("не повторяй обращение" in item["content"] for item in messages))
        self.assertTrue(any("не переноси названия" in item["content"].casefold() for item in messages))
        self.assertTrue(any(item == {"role": "assistant", "content": "Правовую основу я нашёл"} for item in messages))
        self.assertEqual(messages[-1], {"role": "user", "content": "Теперь составь полную речь"})
        self.assertEqual(result["response_mode"], "creative")
        self.assertEqual(result["citations"][0]["source_id"], 7)

    async def test_legal_answer_uses_generated_research_contract(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat",
            chat_model="openai/gpt-5-mini",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        source = {
            "source_id": 8,
            "title": "Процессуальный кодекс",
            "url": "https://example.test/process",
            "text": "Обыск проводится при наличии предусмотренного кодексом основания.",
            "knowledge_domain": "ic",
            "corpus_kind": "law",
            "score": 0.94,
        }
        catalog = [{
            "id": 8,
            "title": "Процессуальный кодекс",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }]

        async def complete(_method, _url, *, payload, **_kwargs):
            if payload.get("response_format"):
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps(
                                {
                                    "resolved_question": "Проверить законность обыска по ордеру",
                                    "search_queries": [
                                        "Процессуальный кодекс основания обыска",
                                        "исключения и пределы ордера на обыск",
                                    ],
                                    "verification_points": [
                                        "основание обыска",
                                        "компетенция выдавшего ордер",
                                    ],
                                    "uncertainties": ["кто выдал ордер"],
                                    "answer_strategy": "Дать вывод и перечислить проверяемые условия",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }]
                }
            return {"choices": [{"message": {"content": "Законность зависит от основания [1]."}}]}

        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=catalog,
        ), patch(
            "modules.atlas_ai.atlas_search",
            AsyncMock(return_value=[source]),
        ) as search, patch(
            "modules.atlas_ai._json_request",
            side_effect=complete,
        ) as request:
            result = await atlas_answer(
                77,
                "Мне выдали ордер и провели обыск. Это было законно?",
            )

        self.assertEqual(request.await_count, 2)
        self.assertIn(
            "исключения и пределы ордера на обыск",
            search.await_args.kwargs["query_variants"],
        )
        self.assertIn(
            "Проверить законность обыска по ордеру. Проверить: основание обыска",
            search.await_args.kwargs["query_variants"],
        )
        self.assertEqual(result["intelligence"]["source"], "generated")
        self.assertEqual(result["evidence"]["matched_checks"], ["основание обыска"])
        self.assertEqual(
            result["evidence"]["open_checks"],
            ["компетенция выдавшего ордер"],
        )
        self.assertEqual(result["citation_health"]["status"], "ok")
        final_payload = request.await_args.kwargs["payload"]
        self.assertTrue(
            any("ИССЛЕДОВАТЕЛЬСКАЯ КАРТА" in item["content"] for item in final_payload["messages"])
        )
        self.assertTrue(
            any("КАРТА ДОКАЗАТЕЛЬСТВ ATLAS" in item["content"] for item in final_payload["messages"])
        )

    async def test_balanced_answer_can_help_when_search_has_no_confirmed_source(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat",
            chat_model="test/model",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        response = {"choices": [{"message": {"content": "Могу предложить творческий черновик."}}]}
        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai.atlas_search",
            AsyncMock(return_value=[]),
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value=response),
        ) as request:
            result = await atlas_answer(77, "Помоги составить вступление к речи")

        self.assertEqual(result["answer"], "Могу предложить творческий черновик.")
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["citation_health"]["status"], "no_sources")
        self.assertEqual(result["requested_response_mode"], "balanced")
        self.assertEqual(result["response_mode"], "creative")
        self.assertEqual(request.await_args.kwargs["payload"]["temperature"], 0.68)
        self.assertIn(
            "не отвечай шаблонным отказом о библиотеке",
            request.await_args.kwargs["payload"]["messages"][1]["content"],
        )

    async def test_quick_answer_has_compact_budget_and_non_repetitive_contract(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat",
            chat_model="openai/gpt-5-mini",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        source = {
            "source_id": 97,
            "title": "Уголовный кодекс",
            "url": None,
            "text": "Кодекс определяет преступления и ответственность.",
            "score": 0.9,
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai.atlas_search", AsyncMock(return_value=[source])
        ), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"choices": [{"message": {"content": "Короткий ответ [1]."}}]}),
        ) as request:
            await atlas_answer(77, "Что такое УК?")

        payload = request.await_args.kwargs["payload"]
        system = payload["messages"][0]["content"]
        self.assertLessEqual(payload["max_tokens"], 700)
        self.assertIn("120–220 слов", system)
        self.assertIn("Не используй по привычке постоянные рубрики", system)

    async def test_overlay_answer_skips_planners_and_uses_compact_field_contract(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat",
            chat_model="openai/gpt-5-mini",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        source = {
            "source_id": 97,
            "title": "Процессуальный кодекс",
            "url": None,
            "text": "Сотрудник обязан разъяснить задержанному основание задержания.",
            "score": 0.94,
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai.atlas_search", AsyncMock(return_value=[source])
        ) as search, patch(
            "modules.atlas_ai._build_intelligence_brief", AsyncMock()
        ) as planner, patch(
            "modules.atlas_ai._generate_aristotle_plan", AsyncMock()
        ) as aristotle, patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"choices": [{"message": {"content": "Назовите основание [1]."}}]}),
        ) as request:
            result = await atlas_answer(
                77,
                "Меня задержали, что делать?",
                response_mode="aristotle",
                latency_mode="overlay",
                screen_context="data:image/png;base64,dmFsaWRhdGVk",
                user_profile={"nickname": "Saul Goodman", "rank": "Адвокат"},
            )

        planner.assert_not_awaited()
        aristotle.assert_not_awaited()
        self.assertEqual(search.await_args.kwargs["limit"], 5)
        self.assertTrue(search.await_args.kwargs["expanded"])
        payload = request.await_args.kwargs["payload"]
        self.assertLessEqual(payload["max_tokens"], 180)
        self.assertIn("Полевой интерфейс", payload["messages"][0]["content"])
        self.assertIn("18–36 слов", payload["messages"][0]["content"])
        self.assertIsInstance(payload["messages"][-1]["content"], list)
        self.assertEqual(result["latency_mode"], "overlay")
        self.assertTrue(result["screen_context_used"])
        self.assertEqual(result["depth"], "quick")

    async def test_overlay_greeting_skips_retrieval_and_screen_analysis(self) -> None:
        config = AtlasAIConfig(
            openrouter_key="test",
            openrouter_url="https://openrouter.test/chat",
            chat_model="openai/gpt-5-mini",
            embedding_model="test/embed",
            qdrant_url="http://qdrant",
            qdrant_key="",
            collection="atlas",
            referer="",
            title="Atlas",
        )
        with patch("modules.atlas_ai.atlas_ai_config", return_value=config), patch(
            "modules.atlas_ai.atlas_search", AsyncMock()
        ) as search, patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"choices": [{"message": {"content": "Привет! Что на уме?"}}]}),
        ) as request:
            result = await atlas_answer(
                77,
                "Привет!",
                latency_mode="overlay",
                screen_context="data:image/png;base64,dmFsaWRhdGVk",
            )

        search.assert_not_awaited()
        system = request.await_args.kwargs["payload"]["messages"][0]["content"]
        self.assertIn("Обычное общение", system)
        self.assertFalse(result["screen_context_used"])
        self.assertEqual(result["intent"], "social")

    def test_overlay_answer_hard_bound_prefers_complete_sentence(self) -> None:
        long_answer = (
            "Сначала остановитесь и уточните основание задержания [1]. "
            + "Это второстепенная подробность, которую оверлей не обязан озвучивать. " * 30
        )
        compact = _compact_overlay_answer(long_answer)

        self.assertLessEqual(len(compact), 461)
        self.assertLessEqual(len(compact.split()), 42)
        self.assertTrue(compact.startswith("Сначала остановитесь"))
        self.assertTrue(compact.endswith("…"))


class AtlasKnowledgeFileTests(unittest.TestCase):
    def test_plain_text_and_docx_are_extracted(self) -> None:
        plain = atlas_extract_knowledge_file(
            "Правила.md",
            "Проверенный материал для базы знаний Atlas длиной больше двадцати символов.".encode(),
        )
        document = Document()
        document.add_heading("Устав GOV", level=1)
        document.add_paragraph("Проверенный порядок работы государственного органа.")
        stream = io.BytesIO()
        document.save(stream)
        docx = atlas_extract_knowledge_file("Устав.docx", stream.getvalue())

        self.assertEqual(plain["title"], "Правила")
        self.assertIn("Устав GOV", docx["content"])
        self.assertIn("государственного органа", docx["content"])

    def test_unsupported_and_empty_files_are_rejected(self) -> None:
        with self.assertRaisesRegex(AtlasKnowledgeFileError, "Поддерживаются"):
            atlas_extract_knowledge_file("archive.zip", b"content")
        with self.assertRaisesRegex(AtlasKnowledgeFileError, "пуст"):
            atlas_extract_knowledge_file("empty.txt", b"")


class AtlasWebSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlay_transcribe_accepts_raw_webm_and_returns_compatibility_fields(self) -> None:
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=SimpleNamespace(
                id=42,
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        transcriber = SimpleNamespace(
            configured=True,
            transcribe_pcm=lambda _pcm: "Атлас, что мне делать?",
        )
        app = web.Application()
        with patch("modules.atlas_web.OpenRouterTranscriber", return_value=transcriber):
            register_atlas_web_routes(
                app,
                SimpleNamespace(get_guild=lambda guild_id: None),
                guild_id=77,
                asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
                authenticate=authenticate,
            )
        with patch(
            "modules.atlas_web._decode_overlay_audio",
            AsyncMock(return_value=b"\0" * (48_000 * 2 * 2)),
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    "/api/atlas/overlay/transcribe",
                    data=b"browser-webm",
                    headers={
                        "Content-Type": "audio/webm;codecs=opus",
                        "X-CSRF-Token": "admin-csrf",
                    },
                )
                payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["text"], "Атлас, что мне делать?")
        self.assertEqual(payload["transcript"], payload["text"])
        self.assertEqual(payload["duration_ms"], 1000)

    async def test_overlay_transcribe_rejects_oversized_raw_audio_before_decode(self) -> None:
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=SimpleNamespace(
                id=42,
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application(client_max_size=8 * 1024**2)
        with patch(
            "modules.atlas_web.OpenRouterTranscriber",
            return_value=SimpleNamespace(configured=True),
        ):
            register_atlas_web_routes(
                app,
                SimpleNamespace(get_guild=lambda guild_id: None),
                guild_id=77,
                asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
                authenticate=authenticate,
            )
        with patch(
            "modules.atlas_web._decode_overlay_audio",
            AsyncMock(),
        ) as decode:
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    "/api/atlas/overlay/transcribe",
                    data=io.BytesIO(b"x" * (6 * 1024**2 + 1)),
                    headers={
                        "Content-Type": "audio/webm",
                        "X-CSRF-Token": "admin-csrf",
                    },
                )
                payload = await response.json()

        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"], "atlas_overlay_audio_size_invalid")
        decode.assert_not_awaited()

    async def test_overlay_context_is_csrf_protected_and_stream_uses_owned_character_scope(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-overlay-web-test.db"
        storage.init_db()
        character = storage.add_profile_character(77, 42, "Saul Goodman", "263345")
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=SimpleNamespace(
                id=42,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        async def stream_answer(_organization_id, _question, *, on_delta, **kwargs):
            await on_delta("Полевой ответ [1].")
            stream_answer.kwargs = kwargs
            return {
                "answer": "Полевой ответ [1].",
                "citations": [],
                "model": "atlas-tvr-a",
                "response_mode": "balanced",
                "requested_response_mode": "balanced",
                "latency_mode": "overlay",
                "latency_ms": 8,
            }

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        try:
            with patch("modules.atlas_web.atlas_answer_stream", stream_answer):
                async with TestClient(TestServer(app)) as client:
                    rejected = await client.post(
                        "/api/atlas/overlay/context",
                        json={
                            "character_id": character.id,
                            "server_code": "phoenix-15",
                            "faction_code": "fib",
                        },
                    )
                    voice_rejected = await client.post(
                        "/api/atlas/overlay/transcribe",
                        data=FormData(),
                    )
                    configured = await client.post(
                        "/api/atlas/overlay/context",
                        json={
                            "character_id": character.id,
                            "server_code": "phoenix-15",
                            "faction_code": "fib",
                            "rank": "Special Agent",
                            "screen_context_enabled": True,
                        },
                        headers={"X-CSRF-Token": "admin-csrf"},
                    )
                    context = await client.get("/api/atlas/overlay/context")
                    streamed = await client.post(
                        "/api/atlas/chat/stream",
                        json={
                            "question": "Что делать при задержании?",
                            "latency_mode": "overlay",
                            "character_id": character.id,
                            "faction_code": "lspd",
                            "screen_context": "data:image/png;base64,"
                            + base64.b64encode(b"\x89PNG\r\n\x1a\nframe").decode(),
                        },
                        headers={
                            "X-CSRF-Token": "admin-csrf",
                            "X-Idempotency-Key": "overlay-stream-1",
                        },
                    )
                    context_payload = await context.json()
                    await streamed.text()

            self.assertEqual(rejected.status, 403)
            self.assertEqual(voice_rejected.status, 403)
            self.assertEqual(configured.status, 200)
            self.assertEqual(context_payload["selected_character"]["faction_code"], "fib")
            self.assertNotIn("csrf_token", context_payload)
            self.assertEqual(streamed.status, 200)
            self.assertEqual(stream_answer.kwargs["faction_code"], "fib")
            self.assertEqual(stream_answer.kwargs["latency_mode"], "overlay")
            self.assertEqual(stream_answer.kwargs["user_profile"]["nickname"], "Saul Goodman")
            self.assertTrue(stream_answer.kwargs["screen_context"].startswith("data:image/png"))
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()

    async def test_empty_chat_question_is_rejected_before_ai_or_stream_start(self) -> None:
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=SimpleNamespace(
                id=42,
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        headers = {"X-CSRF-Token": "admin-csrf"}
        with patch("modules.atlas_web.atlas_answer", AsyncMock()) as answer, patch(
            "modules.atlas_web.atlas_answer_stream", AsyncMock()
        ) as stream:
            async with TestClient(TestServer(app)) as client:
                regular = await client.post(
                    "/api/atlas/chat",
                    json={"question": "   "},
                    headers={**headers, "X-Idempotency-Key": "empty-regular"},
                )
                streamed = await client.post(
                    "/api/atlas/chat/stream",
                    json={"question": ""},
                    headers={**headers, "X-Idempotency-Key": "empty-stream"},
                )
                regular_payload = await regular.json()
                streamed_payload = await streamed.json()

        self.assertEqual(regular.status, 400)
        self.assertEqual(streamed.status, 400)
        self.assertEqual(regular_payload["error"], "question_required")
        self.assertEqual(streamed_payload["error"], "question_required")
        answer.assert_not_awaited()
        stream.assert_not_awaited()

    async def test_atlas_surface_is_registered_and_api_requires_login(self) -> None:
        bot = SimpleNamespace(get_guild=lambda guild_id: None)
        app = create_consensus_web_app(bot, guild_id=77)
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/atlas")
            bootstrap = await client.get("/api/atlas/bootstrap")
            desktop_page = await client.get(
                "/atlas",
                headers={"User-Agent": "T-Mod QA TModDesktop/0.3.5"},
            )
            public_api = await client.get(
                "/api/atlas/bootstrap",
                headers={"Host": "atlas.tvr.lat"},
            )
            desktop_api = await client.get(
                "/api/atlas/bootstrap",
                headers={
                    "Host": "atlas.tvr.lat",
                    "User-Agent": "T-Mod QA TModDesktop/0.3.5",
                },
            )

            self.assertEqual(page.status, 200)
            self.assertIn("T-Mod Atlas", await page.text())
            self.assertIn("Только T-Mod Desktop", await page.text())
            self.assertIn('id="atlas-app"', await desktop_page.text())
            self.assertEqual(bootstrap.status, 401)
            self.assertIn((await bootstrap.json())["error"], {"unauthorized", "atlas_login_required"})
            self.assertEqual(public_api.status, 403)
            self.assertEqual((await public_api.json())["error"], "atlas_desktop_required")
            self.assertEqual(desktop_api.status, 401)

    async def test_non_admin_receives_only_closed_preview(self) -> None:
        member = SimpleNamespace(
            id=42,
            display_name="Участник",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Участник",
            csrf_token="preview-csrf",
            member=member,
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/atlas/bootstrap")
            payload = await response.json()
            forbidden = await client.post(
                "/api/atlas/documents",
                json={"title": "Скрытый документ"},
                headers={"X-CSRF-Token": "preview-csrf"},
            )

        self.assertTrue(payload["preview"])
        self.assertEqual(payload["catalog"]["servers"][0]["label"], "Phoenix (15)")
        self.assertEqual(
            {item["code"] for item in payload["catalog"]["factions"]},
            {"lspd", "lscsd", "fib", "gov", "sang", "ems", "wn"},
        )
        self.assertNotIn("documents", payload)
        self.assertEqual(forbidden.status, 403)

    async def test_admin_can_create_and_read_continuity_timeline(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-timeline-web-test.db"
        storage.init_db()
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=SimpleNamespace(
                id=42,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        try:
            async with TestClient(TestServer(app)) as client:
                headers = {
                    "X-CSRF-Token": "admin-csrf",
                    "X-Idempotency-Key": "timeline-event-1",
                }
                created = await client.post(
                    "/api/atlas/timeline",
                    json={
                        "title": "Ситуация у штаба",
                        "event_kind": "incident",
                        "importance": "critical",
                        "summary": "Контекст сохранён для дальнейшего дела.",
                    },
                    headers=headers,
                )
                repeated = await client.post(
                    "/api/atlas/timeline",
                    json={"title": "Не должно дублироваться"},
                    headers=headers,
                )
                listed = await client.get("/api/atlas/timeline")
                payload = await listed.json()

                patched = await client.patch(
                    f"/api/atlas/timeline/{payload['items'][0]['id']}",
                    json={"status": "resolved", "expected_version": 1},
                    headers={"X-CSRF-Token": "admin-csrf"},
                )
                patched_payload = await patched.json()

            self.assertEqual(created.status, 201)
            self.assertEqual(repeated.status, 201)
            self.assertEqual(payload["summary"]["attention"], 1)
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(payload["items"][0]["title"], "Ситуация у штаба")
            self.assertIsNone(payload["next_cursor"])
            self.assertEqual(patched.status, 200)
            self.assertEqual(patched_payload["event"]["status"], "resolved")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()

    async def test_admin_can_upload_scoped_knowledge_file(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-web-test.db"
        storage.init_db()
        member = SimpleNamespace(
            id=42,
            display_name="Администратор",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=member,
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application(client_max_size=10 * 1024 * 1024)
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        form = FormData()
        form.add_field("server_code", "phoenix-15")
        form.add_field("faction_code", "gov")
        form.add_field("visibility_scope", "server")
        form.add_field("source_kind", "regulation")
        form.add_field(
            "file",
            "Проверенный регламент Government для Atlas AI длиной больше двадцати символов.".encode(),
            filename="Регламент GOV.txt",
            content_type="text/plain",
        )
        try:
            with patch("modules.atlas_web.atlas_index_source", AsyncMock(return_value=["point-1"])):
                async with TestClient(TestServer(app)) as client:
                    uploaded = await client.post(
                        "/api/atlas/knowledge/upload",
                        data=form,
                        headers={"X-CSRF-Token": "admin-csrf", "X-Idempotency-Key": "upload-1"},
                    )
                    self.assertEqual(uploaded.status, 202, await uploaded.text())
                    await asyncio.sleep(0.05)
                    listed = await client.get(
                        "/api/atlas/knowledge?server_code=phoenix-15&faction_code=gov"
                    )
                    payload = await listed.json()
            self.assertEqual(payload["items"][0]["faction_code"], "gov")
            self.assertEqual(payload["items"][0]["visibility_scope"], "server")
            self.assertEqual(payload["items"][0]["original_filename"], "Регламент GOV.txt")
            self.assertEqual(payload["items"][0]["status"], "indexed")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()

    async def test_admin_can_import_and_auto_classify_authenticated_forum_thread(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-forum-import-test.db"
        storage.init_db()
        member = SimpleNamespace(
            id=42,
            display_name="Администратор",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=member,
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        snapshot = AtlasForumSnapshot(
            url="https://forum.majestic-rp.ru/threads/ustav-gov.700/",
            title="Устав GOV",
            content="Настоящий устав определяет полномочия и порядок службы Government.",
            author="Author",
        )
        listing_snapshot = AtlasForumSnapshot(
            url="https://forum.majestic-rp.ru/threads/general-rule.701/",
            title="Общие правила сервера",
            content="Полная редакция общих правил сервера для всех организаций Phoenix.",
            author="Forum Admin",
        )
        try:
            with patch(
                "modules.atlas_forum_sync.AtlasForumSyncRunner.fetch_thread",
                AsyncMock(return_value=snapshot),
            ) as fetch_thread, patch(
                "modules.atlas_forum_sync.AtlasForumSyncRunner.trigger",
                return_value=True,
            ) as trigger, patch(
                "modules.atlas_forum_sync.AtlasForumSyncRunner.fetch_listing",
                AsyncMock(
                    return_value=AtlasForumScrapeBatch((listing_snapshot,), True)
                ),
            ) as fetch_listing, patch(
                "modules.atlas_web.atlas_index_source",
                AsyncMock(return_value=["point-1"]),
            ):
                async with TestClient(TestServer(app)) as client:
                    response = await client.post(
                        "/api/atlas/knowledge/import-forum",
                        json={
                            "source_url": snapshot.url,
                            "server_code": "phoenix-15",
                            "faction_code": "gov",
                            "visibility_scope": "faction",
                        },
                        headers={
                            "X-CSRF-Token": "admin-csrf",
                            "X-Idempotency-Key": "forum-import-1",
                        },
                    )
                    payload = await response.json()
                    bulk_response = await client.post(
                        "/api/atlas/knowledge/import-forum",
                        json={
                            "source_url": (
                                "https://forum.majestic-rp.ru/forums/"
                                "zakonodatel-naya-baza.1213/"
                            )
                        },
                        headers={
                            "X-CSRF-Token": "admin-csrf",
                            "X-Idempotency-Key": "forum-import-bulk-1",
                        },
                    )
                    bulk_payload = await bulk_response.json()
                    general_response = await client.post(
                        "/api/atlas/knowledge/import-forum",
                        json={
                            "source_url": (
                                "https://forum.majestic-rp.ru/forums/"
                                "general-server-rules/"
                            ),
                            "server_code": "phoenix-15",
                            "faction_code": "gov",
                            "visibility_scope": "server",
                        },
                        headers={
                            "X-CSRF-Token": "admin-csrf",
                            "X-Idempotency-Key": "forum-import-general-1",
                        },
                    )
                    general_payload = await general_response.json()
                    await asyncio.sleep(0.1)

            self.assertEqual(response.status, 202, payload)
            self.assertTrue(payload["queued"])
            self.assertEqual(payload["job"]["job_type"], "atlas.forum.thread.v1")
            fetch_thread.assert_awaited_once_with(snapshot.url)
            self.assertEqual(bulk_response.status, 202, bulk_payload)
            self.assertTrue(bulk_payload["bulk"])
            trigger.assert_called_once_with()
            self.assertEqual(general_response.status, 202, general_payload)
            self.assertTrue(general_payload["bulk"])
            self.assertIn("каждую тему", general_payload["message"])
            fetch_listing.assert_awaited_once_with(
                "https://forum.majestic-rp.ru/forums/general-server-rules/"
            )
            sources = atlas_repository.atlas_searchable_knowledge_sources(
                int(payload["job"]["organization_id"]),
                server_code="phoenix-15",
                faction_code="gov",
            )
            by_title = {item["title"]: item for item in sources}
            self.assertIn("Общие правила сервера", by_title)
            self.assertIn("Устав GOV", by_title)
            self.assertEqual(by_title["Устав GOV"]["faction_code"], "gov")
            self.assertEqual(by_title["Устав GOV"]["metadata"]["taxonomy"]["domain"], "ic")
            self.assertEqual(
                by_title["Устав GOV"]["metadata"]["taxonomy"]["corpus_kind"],
                "charter",
            )
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()

    async def test_chat_endpoint_continues_thread_and_exposes_owned_history(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-chat-test.db"
        storage.init_db()
        member = SimpleNamespace(
            id=42,
            display_name="Администратор",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=member,
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        answer = AsyncMock(
            return_value={
                "answer": "Продолжение ответа",
                "citations": [],
                "model": "test/model",
                "response_mode": "creative",
                "latency_ms": 12,
            }
        )
        async def stream_answer(_organization_id, _question, *, on_delta, **_kwargs):
            await on_delta("Поток")
            await on_delta("овый ответ")
            return {
                "answer": "Потоковый ответ",
                "citations": [],
                "model": "atlas-tvr-a",
                "response_mode": "balanced",
                "requested_response_mode": "balanced",
                "latency_ms": 8,
            }

        headers = {"X-CSRF-Token": "admin-csrf"}
        try:
            with patch("modules.atlas_web.atlas_answer", answer), patch(
                "modules.atlas_web.atlas_answer_stream",
                stream_answer,
            ):
                async with TestClient(TestServer(app)) as client:
                    first = await client.post(
                        "/api/atlas/chat",
                        json={"question": "Подготовь речь", "response_mode": "creative"},
                        headers={**headers, "X-Idempotency-Key": "chat-1"},
                    )
                    first_payload = await first.json()
                    second = await client.post(
                        "/api/atlas/chat",
                        json={
                            "question": "Сделай её короче",
                            "thread_id": first_payload["thread_id"],
                            "response_mode": "creative",
                        },
                        headers={**headers, "X-Idempotency-Key": "chat-2"},
                    )
                    streamed = await client.post(
                        "/api/atlas/chat/stream",
                        json={
                            "question": "Покажи поток",
                            "thread_id": first_payload["thread_id"],
                        },
                        headers={**headers, "X-Idempotency-Key": "chat-3"},
                    )
                    stream_events = [
                        json.loads(line.removeprefix("data:"))
                        for line in (await streamed.text()).splitlines()
                        if line.strip().startswith(("data:", "{"))
                    ]
                    threads = await client.get("/api/atlas/threads")
                    detail = await client.get(
                        f"/api/atlas/threads/{first_payload['thread_id']}"
                    )
                    threads_payload = await threads.json()
                    detail_payload = await detail.json()

            self.assertEqual(first.status, 200)
            self.assertEqual(second.status, 200)
            self.assertEqual(streamed.status, 200)
            self.assertEqual(
                "".join(item.get("text", "") for item in stream_events if item["type"] == "delta"),
                "Потоковый ответ",
            )
            self.assertEqual(stream_events[-1]["type"], "done")
            self.assertEqual(len(threads_payload["items"]), 1)
            self.assertEqual(len(detail_payload["messages"]), 6)
            second_history = answer.await_args_list[1].kwargs["history"]
            self.assertEqual(
                [item["role"] for item in second_history],
                ["user", "assistant"],
            )
            self.assertEqual(answer.await_args_list[1].kwargs["response_mode"], "creative")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()

    async def test_startup_rebuilds_corrupt_search_index_from_saved_sources(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-rebuild-test.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Администратор")
        source = atlas_repository.atlas_add_knowledge(
            int(dashboard["organization"]["id"]),
            42,
            title="Сохранённый регламент",
            content="Проверенный регламент остаётся в Atlas и восстанавливает поисковый индекс.",
        )

        async def authenticate(_request):
            return None, False

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        probe = AsyncMock(return_value={"status": "corrupted", "points_count": None})
        reset = AsyncMock(return_value=None)
        index = AsyncMock(return_value=["restored-point"])
        try:
            with patch("modules.atlas_web.atlas_probe_collection", probe), patch(
                "modules.atlas_web.atlas_reset_collection", reset
            ), patch("modules.atlas_web.atlas_index_source", index):
                async with TestClient(TestServer(app)):
                    for _ in range(50):
                        if index.await_count:
                            break
                        await asyncio.sleep(0.02)

            reset.assert_awaited_once()
            index.assert_awaited_once()
            self.assertEqual(index.await_args.args[0]["id"], source["id"])
            rebuilt = atlas_repository.atlas_knowledge_sources(
                int(dashboard["organization"]["id"])
            )[0]
            self.assertEqual(rebuilt["status"], "indexed")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
