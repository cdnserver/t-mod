import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules import atlas_ai


class AtlasResponseQualityTests(unittest.IsolatedAsyncioTestCase):
    def _config(self) -> atlas_ai.AtlasAIConfig:
        return atlas_ai.AtlasAIConfig(
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

    @staticmethod
    def _scope() -> dict[str, str]:
        return {
            "project_code": "majestic-rp",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "federation_scope": "server",
        }

    def test_social_router_handles_colloquial_greetings_without_faction_prose(self) -> None:
        for question in ("Привет, как дела?", "Привет, Атлас", "как ты?", "что нового", "Здорово"):
            profile = atlas_ai._atlas_task_profile(question, mode="balanced")
            self.assertEqual(profile.intent, "social", question)

    def test_core_term_definition_is_short_and_deterministic(self) -> None:
        prepared = SimpleNamespace(
            intent="legal_analysis",
            payload={"messages": [{"role": "user", "content": "что такое УК?"}]},
        )

        answer = atlas_ai._deterministic_term_reply(prepared)

        self.assertTrue(answer.startswith("УК — Уголовный кодекс"))
        self.assertLess(len(answer.split()), 30)

    def test_visual_router_is_separate_from_legal_retrieval(self) -> None:
        profile = atlas_ai._atlas_task_profile(
            "Что за растение спереди меня?",
            mode="balanced",
        )
        self.assertEqual(profile.intent, "visual")
        self.assertIn("видим", profile.response_brief)

    def test_ordinary_answer_has_a_hard_delivery_ceiling(self) -> None:
        prepared = SimpleNamespace(latency_mode="standard", intent="legal_analysis", depth="standard")
        long_answer = "Прямой вывод. " + "Лишнее пояснение без новой пользы. " * 300
        compact = atlas_ai._compact_answer_for_delivery(prepared, long_answer)
        self.assertLessEqual(len(compact), 1_801)
        self.assertLessEqual(len(compact.split()), 180)
        self.assertTrue(compact.startswith("Прямой вывод."))
        self.assertTrue(compact.endswith("…"))

    def test_overlay_detention_answer_does_not_switch_to_officer_perspective(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="overlay",
            intent="procedural_advice",
            payload={"messages": [{"role": "user", "content": "что делать если меня задержали сотрудники LSPD?"}]},
        )
        answer = (
            "Сохраняйте спокойствие и попросите назвать основание задержания. "
            "Сотрудник должен надеть наручники и уведомить: «Вы задержаны». "
            "Попросите разъяснить права [Источник 1, статья 2.2.1]."
        )

        compact = atlas_ai._reframe_overlay_detainee_answer(prepared, answer)

        self.assertIn("Сохраняйте спокойствие", compact)
        self.assertIn("Попросите разъяснить права", compact)
        self.assertNotIn("надеть наручники", compact.casefold())
        self.assertIn("[Источник 1, статья 2.2.1]", compact)

    def test_overlay_bound_never_cuts_inside_citation(self) -> None:
        value = (
            "Первый шаг завершён и подтверждён [Источник 1, статья 2.2.1]. "
            "Второй шаг содержит дополнительные условия [Источник 2, статья 3.2]."
        )
        compact = atlas_ai._compact_overlay_answer(value, max_words=10, max_chars=460)

        self.assertNotIn("[Источник 2", compact)
        self.assertNotIn("статья 2.…", compact)
        self.assertTrue(compact.endswith("…"))

    def test_overlay_removes_leading_citations_and_incomplete_tail(self) -> None:
        value = (
            "[1, п.2.2.1][1, п.2.2]\n\nКороткие шаги:\n"
            "1) Представьтесь и предъявите документы. [1, п.2.2.3]\n"
            "2) Если вас задержали — ждите"
        )

        clean = atlas_ai._sanitize_overlay_completion(value)

        self.assertFalse(clean.startswith("["))
        self.assertIn("Представьтесь и предъявите документы.", clean)
        self.assertNotIn("Если вас задержали", clean)

        partial = (
            "2.2; п.2.2.1–2.2.4]. Шаги: 1) Предъявите документы и спокойно"
        )
        self.assertEqual(
            atlas_ai._sanitize_overlay_completion(partial),
            "Шаги: 1) Предъявите документы и спокойно",
        )

    async def test_visual_question_without_frame_gets_actionable_short_reply(self) -> None:
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock()) as search, patch(
            "modules.atlas_ai._json_request", AsyncMock()
        ) as request:
            result = await atlas_ai.atlas_answer(77, "Что это за растение?", latency_mode="overlay")

        self.assertEqual(result["intent"], "visual")
        self.assertIn("скриншот", result["answer"])
        self.assertFalse(result["screen_context_used"])
        search.assert_not_awaited()
        request.assert_not_awaited()

    async def test_overlay_visual_question_sends_frame_without_irrelevant_sources(self) -> None:
        provider = {
            "choices": [{"message": {"content": "На кадре зелёное растение с длинными листьями."}}]
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock()) as search, patch(
            "modules.atlas_ai._json_request", AsyncMock(return_value=provider)
        ) as request:
            result = await atlas_ai.atlas_answer(
                77,
                "Что за растение?",
                latency_mode="overlay",
                screen_context="data:image/png;base64,dmFsaWQ=",
            )

        self.assertEqual(result["intent"], "visual")
        self.assertEqual(result["citations"], [])
        self.assertTrue(result["screen_context_used"])
        search.assert_not_awaited()
        prompt = request.await_args.kwargs["payload"]["messages"][0]["content"]
        self.assertIn("Визуальный запрос", prompt)
        self.assertNotIn("Полевой интерфейс", prompt)

    async def test_overlay_screen_question_is_visual_even_without_object_name(self) -> None:
        provider = {
            "choices": [{"message": {"content": "На экране виден игровой интерфейс."}}]
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock()) as search, patch(
            "modules.atlas_ai._json_request", AsyncMock(return_value=provider)
        ) as request:
            result = await atlas_ai.atlas_answer(
                77,
                "Что видно на экране?",
                latency_mode="overlay",
                screen_context="data:image/png;base64,dmFsaWQ=",
            )

        self.assertEqual(result["intent"], "visual")
        self.assertEqual(result["citations"], [])
        search.assert_not_awaited()
        self.assertIn("Визуальный запрос", request.await_args.kwargs["payload"]["messages"][0]["content"])

    async def test_overlay_retry_keeps_compact_token_budget(self) -> None:
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock(return_value=[])), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(
                side_effect=[
                    {"choices": [{"message": {"content": ""}}]},
                    {"choices": [{"message": {"content": "Короткий ответ."}}]},
                ]
            ),
        ) as request:
            result = await atlas_ai.atlas_answer(
                77,
                "Что происходит?",
                latency_mode="overlay",
            )

        self.assertEqual(result["answer"], "Короткий ответ.")
        self.assertLessEqual(request.await_args_list[1].kwargs["payload"]["max_tokens"], 180)

    async def test_retrieval_refusal_is_repaired_before_it_reaches_the_user(self) -> None:
        source = {
            "source_id": 12,
            "title": "Уголовный кодекс",
            "url": None,
            "text": "Статья 10.1. Кража — тайное хищение имущества. [1]",
            "structured": True,
            "reference": "article:10.1",
            "pinpoints": ["статья 10.1"],
            "score": 9.0,
        }
        provider = AsyncMock(
            side_effect=[
                {"choices": [{"message": {"content": "В библиотеке Atlas нет точной статьи."}}]},
                {"choices": [{"message": {"content": "Статья 10.1 применима к тайному хищению [1]."}}]},
            ]
        )
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock(return_value=[source])), patch(
            "modules.atlas_ai._json_request", provider
        ):
            result = await atlas_ai.atlas_answer(77, "Какая статья за кражу?")

        self.assertNotIn("библиотек", result["answer"].casefold())
        self.assertIn("10.1", result["answer"])
        self.assertEqual(provider.await_count, 2)

    async def test_article_for_offence_uses_numbered_clauses_without_model_rewrite(self) -> None:
        source = {
            "source_id": 5,
            "title": "Уголовный кодекс",
            "url": None,
            "text": (
                "10.5\nГрабеж, то есть открытое хищение чужого имущества.\n"
                "Приоритет розыска 3\nНаказание: до 30 месяцев.\n\n"
                "10.6\nРазбойное ограбление — нападение с опасным насилием.\n"
                "Приоритет розыска 5\nНаказание: до 50 месяцев."
            ),
            "structured": True,
            "reference": "article:10.5",
            "pinpoints": ["статья 10.5"],
            "knowledge_domain": "ic",
            "corpus_kind": "law",
            "score": 10.0,
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock(return_value=[source])) as search, patch(
            "modules.atlas_ai._json_request", AsyncMock()
        ) as request:
            result = await atlas_ai.atlas_answer(
                77,
                "Назови точные статьи за грабеж или разбойное ограбление",
            )

        self.assertIn("10.5", result["answer"])
        self.assertIn("10.6", result["answer"])
        self.assertEqual(result["model_provider"], "tmod")
        search.assert_awaited_once()
        request.assert_not_awaited()

    def test_thematic_lookup_ignores_generic_word_matches(self) -> None:
        sources = [
            {
                "id": 3,
                "title": "Уголовный Кодекс штата San Andreas",
                "content_text": "3.15\nПолоса движения имеет ширину, достаточную для движения автомобилей.",
                "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
            },
            {
                "id": 5,
                "title": "Уголовный Кодекс штата San Andreas",
                "content_text": "10.5\nГрабеж, то есть открытое хищение чужого имущества.",
                "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
            },
        ]
        candidates = atlas_ai._atlas_thematic_legal_candidates(
            "Назови точную статью за грабеж",
            sources,
        )
        references = {item["reference"] for item in candidates}
        self.assertIn("article:10.5", references)
        self.assertNotIn("article:3.15", references)

    async def test_thematic_lookup_does_not_wait_for_semantic_search(self) -> None:
        source = {
            "id": 905,
            "organization_id": 1,
            "project_code": "majestic-rp",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "federation_scope": "server",
            "title": "Уголовный Кодекс штата San Andreas",
            "content_text": "10.5 Грабеж — открытое хищение чужого имущества. Наказание: до 30 месяцев.",
            "source_url": "https://forum.majestic-rp.ru/threads/uk.905/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=[source],
        ), patch("modules.atlas_ai.atlas_embed", AsyncMock()) as embed, patch(
            "modules.atlas_ai._json_request", AsyncMock()
        ) as request:
            result = await atlas_ai.atlas_search(
                77,
                "Какая статья за грабеж?",
                expanded=True,
            )

        self.assertEqual(result[0]["reference"], "article:10.5")
        embed.assert_not_awaited()
        request.assert_not_awaited()

    async def test_exact_lookup_does_not_return_wrong_numbered_clause(self) -> None:
        wrong = {
            "source_id": 3,
            "structured": True,
            "reference": "article:3.15",
            "pinpoints": ["статья 3.15"],
            "title": "Уголовный кодекс",
            "url": None,
            "knowledge_domain": "ic",
            "corpus_kind": "law",
            "score": 4.0,
            "text": "3.15 Полоса движения.",
        }
        target = {
            "source_id": 5,
            "structured": True,
            "reference": "article:16",
            "pinpoints": ["статья 16"],
            "title": "Уголовный кодекс",
            "url": None,
            "knowledge_domain": "ic",
            "corpus_kind": "law",
            "score": 9.0,
            "text": "16. Статья о составе преступления.",
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock(return_value=[wrong, target])), patch(
            "modules.atlas_ai._json_request", AsyncMock()
        ) as request:
            result = await atlas_ai.atlas_answer(77, "Напиши полностью статью 16 УК")

        self.assertIn("статья о составе", result["answer"].casefold())
        self.assertNotIn("полоса движения", result["answer"].casefold())
        request.assert_not_awaited()

    async def test_all_refusal_completion_becomes_a_useful_prompt(self) -> None:
        provider = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "В библиотеке Atlas нет данных по этому вопросу."}}]
            }
        )
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock(return_value=[])), patch(
            "modules.atlas_ai._json_request", provider
        ):
            result = await atlas_ai.atlas_answer(77, "Что это за процедура?")

        self.assertNotIn("библиотек", result["answer"].casefold())
        self.assertIn("уточни", result["answer"].casefold())

    async def test_blank_provider_completion_becomes_a_user_facing_prompt(self) -> None:
        provider = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock(return_value=[])), patch(
            "modules.atlas_ai._json_request", provider
        ):
            result = await atlas_ai.atlas_answer(77, "Что происходит?", latency_mode="overlay")

        self.assertNotIn("модель не вернула", result["answer"].casefold())
        self.assertIn("уточни", result["answer"].casefold())

    def test_refusal_led_essay_does_not_leave_speculative_tail(self) -> None:
        prepared = SimpleNamespace(intent="legal_analysis", sources=[], payload={})
        answer = (
            "В предоставленной мне библиотеке источников нет полного текста главы 16. "
            "Тем не менее, эта глава, вероятно, регулирует самые тяжкие составы. "
            "Любые конкретные утверждения будут спекуляцией."
        )

        clean = atlas_ai._answer_without_internal_search_state(prepared, answer)

        self.assertIn("опиши ситуацию", clean.casefold())
        self.assertNotIn("вероятно", clean.casefold())
        self.assertNotIn("спекуляц", clean.casefold())


if __name__ == "__main__":
    unittest.main()
