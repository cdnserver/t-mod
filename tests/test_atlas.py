import asyncio
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
    _atlas_query_variants,
    _chunks,
    atlas_ai_config,
    atlas_answer,
    atlas_answer_stream,
    atlas_ensure_collection,
    atlas_index_source,
    atlas_probe_collection,
    atlas_research_plan,
    atlas_search,
)
from modules.atlas_knowledge import AtlasKnowledgeFileError, atlas_extract_knowledge_file
from modules.atlas_taxonomy import atlas_classify_knowledge
from modules.atlas_forum_sync import AtlasForumSnapshot
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

    def test_onboarding_and_document_are_persisted(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        membership = atlas_repository.atlas_update_onboarding(
            organization_id,
            42,
            step=4,
            profile={"agency": "SGL", "position": "Адвокат"},
        )
        document = atlas_repository.atlas_create_document(
            organization_id,
            42,
            title="Рапорт №1",
            template_id=int(dashboard["templates"][0]["id"]),
            fields={"facts": "Проверено"},
        )

        self.assertEqual(membership["onboarding_step"], 4)
        self.assertEqual(membership["profile"]["agency"], "SGL")
        self.assertEqual(document["title"], "Рапорт №1")
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
    def test_expensive_legacy_default_is_downgraded_to_economy_model(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_OPENROUTER_MODEL": "openai/gpt-5.4"},
        ):
            self.assertEqual(atlas_ai_config().chat_model, "openai/gpt-5-mini")

    def test_custom_atlas_model_is_preserved(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_OPENROUTER_MODEL": "custom/provider-model"},
        ):
            self.assertEqual(atlas_ai_config().chat_model, "custom/provider-model")

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
            chat_model="test/model",
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
        self.assertIn("судебную реформу", search.await_args.args[1])
        self.assertTrue(any("Предпочитаю спокойный" in item["content"] for item in messages))
        self.assertTrue(any(item == {"role": "assistant", "content": "Правовую основу я нашёл"} for item in messages))
        self.assertEqual(messages[-1], {"role": "user", "content": "Теперь составь полную речь"})
        self.assertEqual(result["response_mode"], "creative")
        self.assertEqual(result["citations"][0]["source_id"], 7)

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
        self.assertEqual(result["requested_response_mode"], "balanced")
        self.assertEqual(result["response_mode"], "creative")
        self.assertEqual(request.await_args.kwargs["payload"]["temperature"], 0.68)
        self.assertIn(
            "источников для этого запроса не найдено",
            request.await_args.kwargs["payload"]["messages"][1]["content"],
        )


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
    async def test_atlas_surface_is_registered_and_api_requires_login(self) -> None:
        bot = SimpleNamespace(get_guild=lambda guild_id: None)
        app = create_consensus_web_app(bot, guild_id=77)
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/atlas")
            bootstrap = await client.get("/api/atlas/bootstrap")

            self.assertEqual(page.status, 200)
            self.assertIn("T-Mod Atlas", await page.text())
            self.assertEqual(bootstrap.status, 401)
            self.assertIn((await bootstrap.json())["error"], {"unauthorized", "atlas_login_required"})

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
            {"lspd", "gov"},
        )
        self.assertNotIn("documents", payload)
        self.assertEqual(forbidden.status, 403)

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
        try:
            with patch(
                "modules.atlas_forum_sync.AtlasForumSyncRunner.fetch_thread",
                AsyncMock(return_value=snapshot),
            ), patch(
                "modules.atlas_forum_sync.AtlasForumSyncRunner.trigger",
                return_value=True,
            ) as trigger, patch(
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

            self.assertEqual(response.status, 202, payload)
            self.assertEqual(payload["taxonomy"]["domain"], "ic")
            self.assertEqual(payload["taxonomy"]["corpus_kind"], "charter")
            self.assertEqual(payload["source"]["faction_code"], "gov")
            self.assertEqual(bulk_response.status, 202, bulk_payload)
            self.assertTrue(bulk_payload["bulk"])
            trigger.assert_called_once_with()
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
                    await asyncio.sleep(0.05)

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
