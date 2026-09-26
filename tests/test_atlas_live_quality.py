from __future__ import annotations

import unittest

from scripts.atlas_live_quality import (
    CASES,
    _answer_cites_expected_source,
    _expected_source_cited,
)


class AtlasLiveQualityCaseTests(unittest.TestCase):
    def test_social_greeting_does_not_require_or_report_irrelevant_retrieval(self) -> None:
        case = next(item for item in CASES if item.case_id == "social-greeting")
        self.assertFalse(case.retrieval_required)

    def test_short_factual_cases_have_tight_answer_limits(self) -> None:
        limits = {
            item.case_id: item.max_words
            for item in CASES
            if item.case_id in {"ooc-dm", "ic-murder-article", "ic-theft-article"}
        }
        self.assertEqual(
            limits,
            {"ooc-dm": 90, "ic-murder-article": 80, "ic-theft-article": 80},
        )

    def test_source_backed_cases_require_a_matching_governing_document(self) -> None:
        case = next(item for item in CASES if item.case_id == "ooc-software")
        self.assertTrue(case.retrieval_required)
        self.assertEqual(case.expected_title, "Правила проверки на стороннее ПО")
        self.assertEqual(case.max_words, 80)
        self.assertIn("PermBan", case.required_terms)
        paraphrase = next(item for item in CASES if item.case_id == "ooc-software-paraphrase")
        self.assertTrue(paraphrase.retrieval_required)
        self.assertEqual(paraphrase.expected_title, case.expected_title)
        self.assertEqual(paraphrase.max_words, 80)

    def test_live_answer_gate_requires_its_expected_source_to_be_cited(self) -> None:
        self.assertTrue(
            _expected_source_cited(
                "Правила проверки на стороннее ПО",
                ["Правила проверки на стороннее ПО."],
            )
        )
        self.assertFalse(
            _expected_source_cited(
                "Правила проверки на стороннее ПО",
                ["Основные правила проекта"],
            )
        )
        self.assertTrue(_expected_source_cited("", []))

    def test_live_answer_citation_number_must_map_to_expected_source(self) -> None:
        titles = ["Уголовный Кодекс штата San Andreas", "Дорожный Кодекс"]
        self.assertTrue(
            _answer_cites_expected_source(
                "Кража — статья 10.1 [1, статья 10.1].",
                "Уголовный Кодекс",
                titles,
            )
        )
        self.assertFalse(
            _answer_cites_expected_source(
                "Правило 6.16 [Источник 1, пункт 6.16].",
                "Дорожный Кодекс",
                titles,
            )
        )
        self.assertTrue(
            _answer_cites_expected_source(
                "См. пункт 1.8 [Источник 2, пункт 1.8].",
                "Дорожный Кодекс",
                titles,
            )
        )


if __name__ == "__main__":
    unittest.main()
