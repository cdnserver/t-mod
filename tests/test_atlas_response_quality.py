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
        for question in (
            "Привет, как дела?",
            "Привет, Атлас",
            "как ты?",
            "что нового",
            "Здорово",
            "Здорово, типок, как дела у тебя, расскажи, чё нового?",
        ):
            profile = atlas_ai._atlas_task_profile(question, mode="balanced")
            self.assertEqual(profile.intent, "social", question)

    def test_vehicle_questions_add_a_road_code_search_lane(self) -> None:
        variants = atlas_ai._atlas_query_variants("Что делать если машину эвакуировали?")
        self.assertTrue(
            any("Дорожный Кодекс" in item and "ответственность" in item for item in variants)
        )

    def test_repository_terms_preserve_precise_numeric_identifiers(self) -> None:
        terms = atlas_ai._atlas_repository_query_terms("жалоба на игрока со статиком 228392")

        self.assertIn("228392", terms)

    def test_overlay_content_search_is_reserved_for_precise_identifiers(self) -> None:
        self.assertTrue(atlas_ai._atlas_overlay_content_search_needed("жалоба, статик 228392"))
        self.assertFalse(atlas_ai._atlas_overlay_content_search_needed("что делать при ДТП"))

    def test_colloquial_legal_questions_get_their_governing_document_lane(self) -> None:
        checks = {
            "какие права у адвоката": ("Процессуальный Кодекс", "коллегии адвокатов"),
            "как получить ордер": ("Процессуальный Кодекс",),
            "что делать при ДТП": ("Дорожный Кодекс",),
            "что делать если меня убили без причины": ("Основные правила проекта",),
            "как обжаловать решение": ("Процессуальный Кодекс", "Судебный Кодекс"),
        }
        for question, hints in checks.items():
            variants = atlas_ai._atlas_query_variants(question)
            joined = "\n".join(variants).casefold()
            self.assertTrue(
                any(hint.casefold() in joined for hint in hints),
                question,
            )

    def test_legal_router_recognizes_license_and_ooc_bug_questions(self) -> None:
        license_profile = atlas_ai._atlas_task_profile(
            "Кто может изъять лицензию?",
            mode="balanced",
        )
        bug_profile = atlas_ai._atlas_task_profile(
            "Можно ли использовать баги?",
            mode="balanced",
        )
        self.assertIn(license_profile.intent, {"legal_analysis", "procedural_advice"})
        self.assertIn(bug_profile.intent, {"legal_analysis", "procedural_advice"})

    def test_colloquial_dm_question_enters_ooc_rules_lane(self) -> None:
        question = "Что делать если меня убили без причины?"
        self.assertTrue(atlas_ai._atlas_ooc_question_signal(question))
        variants = atlas_ai._atlas_query_variants(question)
        self.assertTrue(any("OOC правила" in item for item in variants))

    def test_provider_refusal_variants_are_filtered_without_matching_real_negation(self) -> None:
        for answer in (
            "Я не смог найти ответ по этому вопросу.",
            "Мне не удалось обнаружить применимую норму.",
            "В библиотеке Atlas\nнет точной статьи.",
            "Не нашёл нужную статью в материалах.",
            "Недостаточно данных для точного вывода.",
            "Имеющиеся материалы не позволяют определить ответ.",
            "Не располагаю достаточными сведениями для вывода.",
            "В ответе нет доступной информации.",
            "Я не вижу в контексте нужной нормы.",
            "Не удалось установить ответ по имеющимся материалам.",
            "Информации недостаточно для точного вывода.",
            "У меня нет доступа к источникам для ответа.",
            "I was unable to determine the answer from the provided context.",
            "The provided context doesn't include the relevant article text.",
        ):
            self.assertTrue(
                atlas_ai._atlas_answer_is_retrieval_refusal(answer),
                answer,
            )
        self.assertFalse(
            atlas_ai._atlas_answer_is_retrieval_refusal(
                "В статье 6.2 нет отдельного запрета на оказание первой помощи."
            )
        )
        self.assertFalse(
            atlas_ai._atlas_answer_is_retrieval_refusal(
                "По статье 6.2 я не нашёл нарушений в описанном поведении."
            )
        )

    def test_provider_failure_has_local_legal_fallback(self) -> None:
        prepared = SimpleNamespace(
            intent="legal_analysis",
            latency_mode="standard",
            sources=[
                {
                    "structured": True,
                    "text": "10.1 Кража — тайное хищение чужого имущества.",
                    "reference": "article:10.1",
                    "pinpoints": ["статья 10.1"],
                    "corpus_kind": "law",
                }
            ],
            payload={
                "messages": [{"role": "user", "content": "Какая статья за кражу?"}]
            },
        )

        fallback = atlas_ai._provider_failure_fallback(prepared)

        self.assertIn("10.1 Кража", fallback)
        self.assertNotIn("библиотек", fallback.casefold())

    def test_provider_refusal_rescues_relevant_unstructured_source_excerpt(self) -> None:
        prepared = SimpleNamespace(
            intent="procedural_advice",
            payload={
                "messages": [
                    {"role": "user", "content": "Меня задержали, как получить адвоката?"}
                ]
            },
            sources=[
                {
                    "structured": False,
                    "title": "Памятка о задержании",
                    "text": (
                        "При задержании сообщите причину и попросите предоставить адвоката. "
                        "Сотрудник обязан разъяснить порядок дальнейших действий участнику."
                    ),
                    "score": 8.0,
                }
            ],
        )

        fallback = atlas_ai._grounded_refusal_fallback(prepared)

        self.assertIn("адвоката", fallback.casefold())
        self.assertIn("[1]", fallback)
        self.assertLessEqual(len(fallback.split()), 100)
        self.assertNotIn("библиотек", fallback.casefold())

    def test_provider_refusal_rescues_summary_material_not_only_legal_sources(self) -> None:
        prepared = SimpleNamespace(
            intent="summary",
            payload={
                "messages": [
                    {"role": "user", "content": "Кратко перескажи памятку по вступлению"}
                ]
            },
            sources=[
                {
                    "structured": False,
                    "title": "Памятка по вступлению в Товарищество",
                    "text": (
                        "Сначала создайте Т-Мод аккаунт и заполните анкету. "
                        "Затем дождитесь проверки ОВР и приглашения на консенсус."
                    ),
                    "score": 4.2,
                }
            ],
        )

        fallback = atlas_ai._provider_failure_fallback(prepared)

        self.assertIn("Т-Мод аккаунт", fallback)
        self.assertIn("[1]", fallback)
        self.assertNotIn("библиотек", fallback.casefold())

    def test_provider_refusal_rescues_draft_context_without_claiming_library_is_empty(self) -> None:
        prepared = SimpleNamespace(
            intent="drafting",
            payload={
                "messages": [
                    {"role": "user", "content": "Составь обращение по проверке ОВР"}
                ]
            },
            sources=[
                {
                    "structured": False,
                    "title": "Регламент проверки ОВР",
                    "text": (
                        "Заявка передаётся в ОВР для проверки сведений кандидата. "
                        "Решение оформляется после изучения анкеты и материалов."
                    ),
                    "score": 5.1,
                }
            ],
        )

        fallback = atlas_ai._provider_failure_fallback(prepared)

        self.assertIn("ОВР", fallback)
        self.assertIn("[1]", fallback)
        self.assertNotIn("библиотек", fallback.casefold())

    def test_complaint_procedure_is_short_and_local_when_regulation_is_present(self) -> None:
        prepared = SimpleNamespace(
            intent="procedural_advice",
            payload={"messages": [{"role": "user", "content": "Как подать жалобу на игрока?"}]},
            sources=[
                {
                    "text": (
                        "Регламент жалоб-обращений. 1. Каждый игрок имеет право оформить жалобу "
                        "через систему обращений (F2-Обращения). 3. Укажите статический ID и ссылку."
                    )
                }
            ],
        )

        answer = atlas_ai._deterministic_complaint_procedure_reply(prepared)

        self.assertIn("F2", answer)
        self.assertIn("статический ID", answer)
        self.assertLess(len(answer.split()), 45)
        self.assertNotIn("библиотек", answer.casefold())

    def test_incomplete_trailing_list_marker_is_removed(self) -> None:
        answer = "1. Сохраните запись.\n2. Подайте жалобу.\n3."

        clean = atlas_ai._sanitize_incomplete_answer(answer)

        self.assertEqual(clean, "1. Сохраните запись.\n2. Подайте жалобу.")

    def test_bounded_answer_drops_dangling_ellipsis_after_complete_sentence(self) -> None:
        compact = atlas_ai._finish_bounded_answer(
            "Сначала определите вид решения. Затем подайте жалобу в срок.…"
        )

        self.assertEqual(compact, "Сначала определите вид решения. Затем подайте жалобу в срок.")

    def test_only_incomplete_marker_gets_user_facing_fallback(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="standard",
            intent="general",
            depth="standard",
            model_route=SimpleNamespace(provider="openrouter", model="test", release="test"),
            sources=[],
            payload={"messages": [{"role": "user", "content": "Что происходит?"}]},
            project_code="majestic-rp",
            server_code="phoenix-15",
            faction_code="lspd",
            response_mode="balanced",
            requested_response_mode="balanced",
            research_plan=[],
            agent=SimpleNamespace(public=lambda: {}),
            intelligence_brief=None,
            evidence_map=SimpleNamespace(public=lambda: {}),
            screen_context_used=False,
            direct_mode=False,
            fallback_model_route=None,
            started=0.0,
        )

        result = atlas_ai._atlas_answer_result(prepared, "3.")

        self.assertEqual(result["answer"], "Уточни вопрос одним коротким предложением.")

    def test_empty_answer_result_never_exposes_transport_error(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="standard",
            intent="general",
            depth="standard",
            model_route=SimpleNamespace(provider="openrouter", model="test", release="test"),
            sources=[],
            payload={"messages": [{"role": "user", "content": "Что происходит?"}]},
            project_code="majestic-rp",
            server_code="phoenix-15",
            faction_code="lspd",
            response_mode="balanced",
            requested_response_mode="balanced",
            research_plan=[],
            agent=SimpleNamespace(public=lambda: {}),
            intelligence_brief=None,
            evidence_map=SimpleNamespace(public=lambda: {}),
            screen_context_used=False,
            direct_mode=False,
            fallback_model_route=None,
            started=0.0,
        )

        result = atlas_ai._atlas_answer_result(prepared, "")

        self.assertTrue(result["answer"])
        self.assertNotIn("модель не вернула", result["answer"].casefold())
        self.assertNotIn("библиотек", result["answer"].casefold())

    async def test_provider_timeout_does_not_become_an_atlas_5xx_for_legal_query(self) -> None:
        source = {
            "source_id": 33,
            "title": "Процессуальный Кодекс",
            "url": None,
            "text": "2.2 Сотрудник вправе провести установление личности при задержании.",
            "structured": True,
            "reference": "article:2.2",
            "pinpoints": ["статья 2.2"],
            "knowledge_domain": "ic",
            "corpus_kind": "procedure",
            "score": 9.0,
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai._should_build_intelligence_brief", return_value=False), patch(
            "modules.atlas_ai.atlas_search", AsyncMock(return_value=[source])
        ), patch(
            "modules.atlas_ai._completion_with_fallback",
            AsyncMock(
                side_effect=atlas_ai.AtlasAIError(
                    "upstream_unavailable", "timeout", retryable=True
                )
            ),
        ):
            result = await atlas_ai.atlas_answer(
                77,
                "Какие полномочия у сотрудника при задержании?",
            )

        self.assertIn("2.2", result["answer"])
        self.assertNotIn("библиотек", result["answer"].casefold())

    def test_plain_numbered_road_article_is_parsed_for_dtp(self) -> None:
        source = (
            "Дорожный Кодекс штата San Andreas\n"
            "Статья 9. При аварии водитель обязан немедленно остановиться.\n"
            "Наказание: штраф.\n"
            "Статья 10. Иная норма."
        )
        sections = atlas_ai._atlas_numbered_rule_sections(source)
        self.assertTrue(any(number == "9" and "аварии" in text for number, text in sections))

    def test_dtp_routes_to_the_accident_article_not_definitions(self) -> None:
        source = {
            "id": 32,
            "title": "Дорожный Кодекс штата San Andreas",
            "content_text": (
                "Статья 9. При аварии водитель обязан немедленно остановиться. "
                "Если есть пострадавшие, вызвать EMS. Наказание: штраф.\n"
                "Статья 10. Общая норма о документах."
            ),
            "source_url": "https://example.test/road",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        candidates = atlas_ai._atlas_thematic_legal_candidates("Что делать при ДТП?", [source])
        self.assertTrue(any(item["reference"] == "article:9" for item in candidates))

    def test_vehicle_eviction_wording_matches_the_road_clause(self) -> None:
        source = {
            "id": 42,
            "title": "Дорожный Кодекс Штата San Andreas",
            "content_text": (
                "Статья 17.3\nОснования для эвакуации транспортного средства:\n"
                "Парковка с нарушением правил; отсутствие номерного знака или VIN-кода."
            ),
            "source_url": "https://example.test/road",
            "project_code": "majestic-rp",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
        }
        candidates = atlas_ai._atlas_thematic_legal_candidates(
            "Что делать если машину эвакуировали?",
            [source],
        )
        self.assertTrue(any(item["reference"] == "article:17.3" for item in candidates))
        self.assertNotEqual(candidates[0]["reference"], "article:1.5")

    def test_overlay_vehicle_answer_is_short_and_source_bound(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="overlay",
            intent="procedural_advice",
            sources=[
                {
                    "title": "Дорожный Кодекс Штата San Andreas",
                    "reference": "article:17.3",
                }
            ],
            payload={"messages": [{"role": "user", "content": "Что делать если машину эвакуировали?"}]},
        )
        answer = atlas_ai._deterministic_overlay_vehicle_reply(prepared)
        self.assertIn("статья 17.3", answer)
        self.assertNotIn("отдел хранения", answer.casefold())
        self.assertLessEqual(len(answer.split()), 42)

    def test_overlay_unknown_procedure_does_not_invent_a_department(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="overlay",
            intent="procedural_advice",
            sources=[{"title": "Основные правила проекта", "score": 1.2}],
            payload={"messages": [{"role": "user", "content": "Что делать если пропал предмет?"}]},
        )
        answer = atlas_ai._deterministic_overlay_low_evidence_reply(prepared)
        self.assertIn("Уточни", answer)
        self.assertNotIn("библиотек", answer.casefold())

    def test_weak_overlay_clarification_does_not_expose_unrelated_citations(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="overlay",
            intent="procedural_advice",
            depth="quick",
            model_route=SimpleNamespace(
                provider="tmod",
                model="atlas-exact-retrieval",
                release="index-v3",
            ),
            sources=[
                {
                    "source_id": 9,
                    "title": "Случайный документ",
                    "url": "https://example.test/random",
                    "score": 1.1,
                }
            ],
            payload={"messages": [{"role": "user", "content": "Что делать при неизвестном событии?"}]},
            project_code="majestic-rp",
            server_code="phoenix-15",
            faction_code="lspd",
            response_mode="balanced",
            requested_response_mode="balanced",
            research_plan=[],
            agent=SimpleNamespace(public=lambda: {}),
            intelligence_brief=None,
            evidence_map=SimpleNamespace(public=lambda: {}),
            screen_context_used=False,
            direct_mode=False,
            fallback_model_route=None,
            started=0.0,
        )
        result = atlas_ai._atlas_answer_result(
            prepared,
            "Уточни одним сообщением: что именно произошло, где и кто участвовал.",
        )
        self.assertEqual(result["citations"], [])

    def test_core_term_definition_is_short_and_deterministic(self) -> None:
        prepared = SimpleNamespace(
            intent="legal_analysis",
            payload={"messages": [{"role": "user", "content": "что такое УК?"}]},
        )

        answer = atlas_ai._deterministic_term_reply(prepared)

        self.assertTrue(answer.startswith("УК — Уголовный кодекс"))
        self.assertLess(len(answer.split()), 30)

    def test_explicit_missing_article_gets_a_fast_specific_clarification(self) -> None:
        prepared = SimpleNamespace(
            intent="exact_lookup",
            sources=[
                {
                    "text": (
                        "16.1 Первая норма с достаточным текстом.\n"
                        "16.2 Вторая норма с достаточным текстом.\n"
                        "17.1 Другая норма с достаточным текстом."
                    ),
                }
            ],
            payload={"messages": [{"role": "user", "content": "Напиши статью 16 УК"}]},
        )

        answer = atlas_ai._deterministic_missing_reference_reply(prepared)

        self.assertIn("16.1", answer)
        self.assertIn("16.2", answer)
        self.assertNotIn("библиотек", answer.casefold())

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
        self.assertTrue(compact.endswith("."))

    def test_quick_answer_is_tighter_than_standard_delivery(self) -> None:
        prepared = SimpleNamespace(latency_mode="standard", intent="procedural_advice", depth="quick")
        long_answer = "Прямой вывод. " + "Лишнее пояснение без новой пользы. " * 300
        compact = atlas_ai._compact_answer_for_delivery(prepared, long_answer)
        self.assertLessEqual(len(compact.split()), 110)
        self.assertLessEqual(len(compact), 1_101)
        self.assertTrue(compact.startswith("Прямой вывод."))

    def test_quick_answer_drops_dangling_ellipsis_after_finished_sentence(self) -> None:
        prepared = SimpleNamespace(latency_mode="standard", intent="procedural_advice", depth="quick")
        value = "Сначала назовите причину задержания. " + "Дополнительная деталь. " * 200
        compact = atlas_ai._compact_answer_for_delivery(prepared, value)
        self.assertFalse(compact.endswith("…"))
        self.assertTrue(compact.endswith("."))

    def test_obsolete_search_refusals_are_not_reused_from_dialog_history(self) -> None:
        history = atlas_ai._bounded_dialog_messages(
            [
                {"role": "user", "content": "Какая статья за кражу?"},
                {"role": "assistant", "content": "В библиотеке Atlas нет точной статьи."},
                {"role": "user", "content": "Тогда уточни номер."},
                {"role": "assistant", "content": "10.1 — тайное хищение имущества [Источник 1]."},
            ]
        )
        joined = " ".join(item["content"] for item in history)
        self.assertNotIn("В библиотеке Atlas нет", joined)
        self.assertIn("10.1", joined)

    def test_obsolete_search_refusals_are_not_reused_as_cross_chat_memory(self) -> None:
        memory = atlas_ai._cross_chat_context(
            [
                {"role": "assistant", "feedback_rating": "good", "content_text": "В библиотеке Atlas нет нормы."},
                {"role": "assistant", "feedback_rating": "good", "content_text": "10.1 — кража [Источник 1]."},
            ]
        )
        self.assertNotIn("библиотек", memory.casefold())
        self.assertIn("10.1", memory)

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

    def test_detainee_wording_rewrites_bare_officer_clause_in_standard_mode(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="standard",
            intent="procedural_advice",
            payload={"messages": [{"role": "user", "content": "что делать если меня задержали?"}]},
        )
        answer = "При отказе начнётся процедура: надеть наручники и проверить документы."

        clean = atlas_ai._reframe_overlay_detainee_answer(prepared, answer)

        self.assertIn("попросить сотрудника применить наручники", clean)

    def test_overlay_bound_never_cuts_inside_citation(self) -> None:
        value = (
            "Первый шаг завершён и подтверждён [Источник 1, статья 2.2.1]. "
            "Второй шаг содержит дополнительные условия [Источник 2, статья 3.2]."
        )
        compact = atlas_ai._compact_overlay_answer(value, max_words=10, max_chars=460)

        self.assertNotIn("[Источник 2", compact)
        self.assertNotIn("статья 2.…", compact)
        self.assertTrue(compact.endswith("…"))

    def test_overlay_local_exact_article_stays_complete_when_small(self) -> None:
        prepared = SimpleNamespace(
            latency_mode="overlay",
            intent="exact_lookup",
            depth="quick",
            model_route=SimpleNamespace(provider="tmod"),
        )
        value = (
            "17.3\n(F/R)\nОскорбление представителя власти при исполнении.\n"
            "Наказание: до 20 месяцев лишения свободы.\n\n[1, статья 17.3]"
        )
        self.assertEqual(atlas_ai._compact_answer_for_delivery(prepared, value), value)

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

    async def test_retrieval_repair_timeout_never_becomes_a_transport_error(self) -> None:
        prepared = SimpleNamespace(
            intent="legal_analysis",
            latency_mode="standard",
            sources=[
                {
                    "structured": False,
                    "title": "Несвязанный материал",
                    "text": "Общий организационный текст без нормы.",
                    "score": 0.2,
                }
            ],
            payload={
                "messages": [
                    {"role": "user", "content": "Какая статья регулирует редкий случай?"}
                ]
            },
        )
        route = SimpleNamespace(provider="openrouter", model="test", endpoint="https://example.test")
        with patch(
            "modules.atlas_ai._completion_with_fallback",
            AsyncMock(
                side_effect=atlas_ai.AtlasAIError(
                    "upstream_unavailable",
                    "timeout",
                    retryable=True,
                )
            ),
        ):
            answer, _used_route = await atlas_ai._repair_retrieval_refusal(
                prepared,
                "В библиотеке Atlas нет точной статьи.",
                route,
            )

        self.assertNotIn("библиотек", answer.casefold())
        self.assertIn("опиши ситуацию", answer.casefold())

    async def test_repeated_retrieval_refusal_is_replaced_even_without_matching_excerpt(self) -> None:
        prepared = SimpleNamespace(
            intent="summary",
            latency_mode="standard",
            sources=[
                {
                    "structured": False,
                    "title": "Материал без совпадения",
                    "text": "Текст о другой теме, не связанный с запросом.",
                    "score": 0.1,
                }
            ],
            payload={
                "messages": [
                    {"role": "user", "content": "Кратко объясни редкую процедуру"}
                ]
            },
        )
        route = SimpleNamespace(provider="openrouter", model="test", endpoint="https://example.test")
        with patch(
            "modules.atlas_ai._completion_with_fallback",
            AsyncMock(
                return_value=(
                    {
                        "choices": [
                            {"message": {"content": "В предоставленном контексте нет данных."}}
                        ]
                    },
                    route,
                )
            ),
        ):
            answer, _used_route = await atlas_ai._repair_retrieval_refusal(
                prepared,
                "В библиотеке Atlas нет точной статьи.",
                route,
            )

        self.assertNotIn("библиотек", answer.casefold())
        self.assertNotIn("контексте нет", answer.casefold())
        self.assertIn("уточни", answer.casefold())

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

    async def test_document_route_ranks_governing_source_above_neighbouring_articles(self) -> None:
        sources = [
            {
                "id": 11,
                "organization_id": 1,
                "project_code": "majestic-rp",
                "server_code": "phoenix-15",
                "faction_code": "lspd",
                "visibility_scope": "server",
                "federation_scope": "server",
                "title": "Уголовный Кодекс штата San Andreas",
                "content_text": "4.1 Общие положения. Права лиц регулируются законом.",
                "source_url": "https://example.test/criminal",
                "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
            },
            {
                "id": 12,
                "organization_id": 1,
                "project_code": "majestic-rp",
                "server_code": "phoenix-15",
                "faction_code": "lspd",
                "visibility_scope": "server",
                "federation_scope": "server",
                "title": "Закон \"О коллегии адвокатов штата San Andreas\"",
                "content_text": "1.9 Адвокат имеет право на защиту и участие в деле.",
                "source_url": "https://example.test/lawyers",
                "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "law"}},
            },
        ]
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch(
            "modules.atlas_ai.atlas_storage.atlas_searchable_knowledge_sources",
            return_value=sources,
        ), patch("modules.atlas_ai.atlas_embed", AsyncMock(return_value=[[0.1]])), patch(
            "modules.atlas_ai._json_request",
            AsyncMock(return_value={"result": {"points": []}}),
        ):
            result = await atlas_ai.atlas_search(
                77,
                "Какие права у адвоката?",
                expanded=True,
            )

        self.assertTrue(result)
        self.assertIn("коллегии адвокатов", result[0]["title"].casefold())

    async def test_overlay_procedure_with_canonical_hit_does_not_wait_for_semantic_search(self) -> None:
        source = {
            "id": 906,
            "organization_id": 1,
            "project_code": "majestic-rp",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "federation_scope": "server",
            "title": "Процессуальный Кодекс штата San Andreas",
            "content_text": "2.2 Сотрудник вправе провести установление личности при задержании.",
            "source_url": "https://forum.majestic-rp.ru/threads/procedure.906/",
            "metadata": {"taxonomy": {"domain": "ic", "corpus_kind": "procedure"}},
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
                "Что делать, если меня задержали сотрудники LSPD?",
                expanded=True,
            )

        self.assertTrue(result)
        embed.assert_not_awaited()
        request.assert_not_awaited()

    async def test_overlay_detention_uses_compact_local_procedure_answer(self) -> None:
        source = {
            "source_id": 33,
            "title": "Процессуальный Кодекс штата San Andreas",
            "url": "https://forum.example/procedure",
            "text": "2.1 Причина задержания.\n\n2.6 Порядок предоставления адвоката.",
            "structured": True,
            "reference": "",
            "pinpoints": ["статья 2.1", "статья 2.6"],
            "knowledge_domain": "ic",
            "corpus_kind": "procedure",
            "score": 10.0,
        }
        with patch("modules.atlas_ai.atlas_ai_config", return_value=self._config()), patch(
            "modules.atlas_ai.atlas_storage.atlas_resolve_federation_scope",
            return_value=self._scope(),
        ), patch("modules.atlas_ai.atlas_search", AsyncMock(return_value=[source])), patch(
            "modules.atlas_ai._json_request", AsyncMock()
        ) as request:
            result = await atlas_ai.atlas_answer(
                77,
                "Меня задержали сотрудники LSPD. Что делать?",
                latency_mode="overlay",
            )

        self.assertEqual(result["model_provider"], "tmod")
        self.assertIn("назвать причину задержания", result["answer"])
        self.assertIn("предложить адвоката", result["answer"])
        self.assertLessEqual(len(result["answer"].split()), 42)
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
