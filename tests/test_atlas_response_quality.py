import unittest
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


if __name__ == "__main__":
    unittest.main()
