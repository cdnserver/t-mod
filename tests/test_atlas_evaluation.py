import json
import tempfile
import unittest
from pathlib import Path

from modules.atlas_evaluation import (
    AtlasEvaluationError,
    build_evaluation_report,
    evaluate_atlas_result,
    load_evaluation_cases,
)


class AtlasEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _case_path(self, rows: list[dict]) -> Path:
        path = Path(self.temp_dir.name) / "cases.jsonl"
        path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows),
            encoding="utf-8",
        )
        return path

    def test_release_gate_accepts_scoped_answer_with_required_evidence(self) -> None:
        case = load_evaluation_cases(self._case_path([{
            "case_id": "claims-evidence-1",
            "project_code": "majestic-rp",
            "agent_id": "atlas-claims",
            "server_code": "phoenix-15",
            "faction_code": "gov",
            "question": "Подготовь основу иска.",
            "required_terms": ["требование"],
            "forbidden_terms": ["российский ук"],
            "required_source_ids": [41],
            "min_citations": 1,
            "max_words": 60,
        }]))[0]
        result = {
            "answer": "Требование следует изложить по подтверждённой норме [1].",
            "project_code": "majestic-rp",
            "server_code": "phoenix-15",
            "faction_code": "gov",
            "agent": {"id": "atlas-claims"},
            "model": "account/claims-v1",
            "model_provider": "together",
            "model_release": "claims-v1",
            "citations": [{"source_id": 41}],
        }
        report = evaluate_atlas_result(case, result)

        self.assertTrue(report["passed"])
        self.assertTrue(all(report["checks"].values()))

    def test_release_gate_rejects_cross_project_and_unexpected_terms(self) -> None:
        case = load_evaluation_cases(self._case_path([{
            "case_id": "complaint-ooc-1",
            "project_code": "majestic-rp",
            "agent_id": "atlas-complaints",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "question": "Проверь жалобу.",
            "forbidden_terms": ["придуманная статья"],
        }]))[0]
        report = evaluate_atlas_result(case, {
            "answer": "Это придуманная статья.",
            "project_code": "other-project",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "agent": {"id": "atlas-complaints"},
            "model": "base",
            "model_provider": "openrouter",
            "model_release": "base",
            "citations": [],
        })

        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["project_scope"])
        self.assertFalse(report["checks"]["forbidden_terms"])

    def test_release_gate_rejects_cross_server_answer(self) -> None:
        case = load_evaluation_cases(self._case_path([{
            "case_id": "same-project-wrong-server",
            "project_code": "majestic-rp",
            "agent_id": "atlas-tvr-a",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "question": "Какие правила применимы?",
        }]))[0]
        report = evaluate_atlas_result(case, {
            "answer": "Ответ из другого серверного контура.",
            "project_code": "majestic-rp",
            "server_code": "phoenix-16",
            "faction_code": "lspd",
            "agent": {"id": "atlas-tvr-a"},
            "model": "base",
            "model_provider": "openrouter",
            "model_release": "base",
            "citations": [],
        })

        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["server_scope"])

    def test_case_file_rejects_duplicate_or_invalid_scope(self) -> None:
        duplicate = self._case_path([
            {
                "case_id": "same-case", "project_code": "majestic-rp",
                "agent_id": "atlas-tvr-a", "server_code": "phoenix-15",
                "faction_code": "lspd", "question": "Первый вопрос",
            },
            {
                "case_id": "same-case", "project_code": "majestic-rp",
                "agent_id": "atlas-tvr-a", "server_code": "phoenix-15",
                "faction_code": "lspd", "question": "Второй вопрос",
            },
        ])
        with self.assertRaisesRegex(AtlasEvaluationError, "atlas_eval_case_duplicate"):
            load_evaluation_cases(duplicate)

        invalid = self._case_path([{
            "case_id": "bad case", "project_code": "majestic-rp",
            "agent_id": "atlas-tvr-a", "server_code": "phoenix-15",
            "faction_code": "lspd", "question": "Вопрос",
        }])
        with self.assertRaisesRegex(AtlasEvaluationError, "atlas_eval_case_id_invalid"):
            load_evaluation_cases(invalid)

    def test_report_fails_closed_when_a_case_has_no_result(self) -> None:
        case = load_evaluation_cases(self._case_path([{
            "case_id": "missing-result", "project_code": "majestic-rp",
            "agent_id": "atlas-tvr-a", "server_code": "phoenix-15",
            "faction_code": "lspd", "question": "Вопрос",
        }]))
        report = build_evaluation_report(case, {})

        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
