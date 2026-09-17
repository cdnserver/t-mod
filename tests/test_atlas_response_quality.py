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
        for question in ("Привет, как дела?", "как ты?", "что нового", "Здорово"):
            profile = atlas_ai._atlas_task_profile(question, mode="balanced")
            self.assertEqual(profile.intent, "social", question)

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


if __name__ == "__main__":
    unittest.main()
