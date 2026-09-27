import csv
import json
import tempfile
import unittest
from pathlib import Path

from modules.atlas_finetuning import (
    AtlasDatasetScopeError,
    atlas_agent_system_prompt,
    build_finetuning_bundle,
    prepare_candidates,
    redact_training_text,
    split_approved,
)


def candidate(
    feedback_id: int,
    thread_id: int,
    prompt: str,
    answer: str,
    *,
    project_code: str = "majestic-rp",
    agent_id: str = "atlas-tvr-a",
) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "thread_id": thread_id,
        "project_code": project_code,
        "agent_id": agent_id,
        "user_text": prompt,
        "assistant_text": answer,
        "model": "test-model",
        "model_provider": "openrouter",
        "model_release": "base",
        "answer_server_code": "phoenix-15",
        "answer_faction_code": "lspd",
        "citations": [{"title": "Источник"}],
    }


class AtlasFinetuningTests(unittest.TestCase):
    def test_redacts_credentials_and_direct_identifiers(self) -> None:
        text, labels = redact_training_text(
            "Почта user@example.com, Discord 721577061143019555, "
            "x-api-key=super-secret-value, IP 10.8.0.3"
        )
        self.assertNotIn("user@example.com", text)
        self.assertNotIn("721577061143019555", text)
        self.assertNotIn("super-secret-value", text)
        self.assertNotIn("10.8.0.3", text)
        self.assertEqual(set(labels), {"discord_id", "email", "ip_address", "secret"})

    def test_deduplicates_and_rejects_retrieval_refusal_answers(self) -> None:
        rows = [
            candidate(1, 10, "Что такое УК?", "Этой информации нет в библиотеке Atlas."),
            candidate(2, 11, "Что такое УК?", "В библиотеке Atlas\nнет точной статьи."),
            candidate(3, 12, "Что такое УК?", "УК — это Уголовный кодекс штата San Andreas."),
            candidate(4, 13, "Что такое УК?", "УК — это Уголовный кодекс штата San Andreas."),
        ]
        prepared, rejected = prepare_candidates(rows)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].candidate_id, 3)
        self.assertEqual(rejected["retrieval_refusal"], 2)
        self.assertEqual(rejected["duplicate"], 1)

    def test_keeps_whole_conversations_out_of_training_split(self) -> None:
        prepared, _ = prepare_candidates(
            [
                candidate(1, 10, "Вопрос один", "Подробный корректный ответ номер один."),
                candidate(2, 10, "Вопрос два", "Подробный корректный ответ номер два."),
                candidate(3, 20, "Вопрос три", "Подробный корректный ответ номер три."),
                candidate(4, 30, "Вопрос четыре", "Подробный корректный ответ номер четыре."),
            ]
        )
        train, evaluation = split_approved(prepared, {1, 2, 3, 4})
        train_threads = {item.thread_key for item in train}
        eval_threads = {item.thread_key for item in evaluation}
        self.assertTrue(train)
        self.assertTrue(evaluation)
        self.assertTrue(train_threads.isdisjoint(eval_threads))

    def test_bundle_requires_explicit_human_approval(self) -> None:
        rows = [candidate(1, 10, "Составь речь", "Готовая содержательная речь для выступления.")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = build_finetuning_bundle(rows, root)
            self.assertEqual(manifest["candidate_count"], 1)
            self.assertEqual(manifest["train_count"], 0)
            self.assertFalse(manifest["provider_upload_performed"])
            self.assertEqual((root / "train.jsonl").read_text(encoding="utf-8"), "")

            with (root / "review.csv").open(encoding="utf-8-sig") as handle:
                review = list(csv.DictReader(handle))
            review[0]["status"] = "approved"
            approval_path = root / "approved.csv"
            with approval_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=review[0].keys())
                writer.writeheader()
                writer.writerows(review)
            manifest = build_finetuning_bundle(rows, root / "approved", approvals_path=approval_path)
            self.assertEqual(manifest["approved_count"], 1)
            record = json.loads((root / "approved/train.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["messages"][-1]["role"], "assistant")

    def test_scope_filters_keep_project_and_agent_lanes_separate(self) -> None:
        rows = [
            candidate(
                1,
                10,
                "Подготовь иск",
                "Подробный проект иска с хронологией и требованиями.",
                agent_id="atlas-claims",
            ),
            candidate(
                2,
                20,
                "Составь жалобу",
                "Подробная OOC-жалоба с описанием доказательств.",
                agent_id="atlas-complaints",
            ),
            candidate(
                3,
                30,
                "Вопрос второго проекта",
                "Подробный ответ, относящийся только ко второму проекту.",
                project_code="project-b",
                agent_id="atlas-claims",
            ),
        ]
        prepared, rejected = prepare_candidates(
            rows,
            project_code="majestic-rp",
            agent_id="atlas-claims",
        )

        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].project_code, "majestic-rp")
        self.assertEqual(prepared[0].agent_id, "atlas-claims")
        self.assertEqual(prepared[0].training_lane, "ic-claims")
        self.assertEqual(prepared[0].model_provider, "openrouter")
        self.assertEqual(prepared[0].server_code, "phoenix-15")
        self.assertEqual(rejected["agent_scope_mismatch"], 1)
        self.assertEqual(rejected["project_scope_mismatch"], 1)

    def test_bundle_refuses_mixed_project_or_agent_data_without_explicit_boundary(self) -> None:
        rows = [
            candidate(1, 10, "Первый проект", "Достаточно длинный ответ первого проекта."),
            candidate(
                2,
                20,
                "Второй проект",
                "Достаточно длинный ответ второго проекта.",
                project_code="project-b",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                AtlasDatasetScopeError,
                "atlas_finetuning_cross_project_export_forbidden",
            ):
                build_finetuning_bundle(rows, Path(directory))

    def test_approval_is_bound_to_the_redacted_pair_that_was_reviewed(self) -> None:
        original = [
            candidate(1, 10, "Составь речь", "Готовая содержательная речь для выступления.")
        ]
        changed = [
            candidate(1, 10, "Составь речь", "Другой содержательный ответ после редактирования.")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_finetuning_bundle(original, root / "review")
            with (root / "review" / "review.csv").open(encoding="utf-8-sig") as handle:
                review = list(csv.DictReader(handle))
            review[0]["status"] = "approved"
            approval_path = root / "approved.csv"
            with approval_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=review[0].keys())
                writer.writeheader()
                writer.writerows(review)

            manifest = build_finetuning_bundle(
                changed,
                root / "changed",
                approvals_path=approval_path,
            )
            self.assertEqual(manifest["approved_count"], 0)
            self.assertTrue(any("checksum" in warning for warning in manifest["warnings"]))

    def test_agent_prompt_preserves_specialist_boundary(self) -> None:
        complaint_prompt = atlas_agent_system_prompt("atlas-complaints")
        self.assertIn("OOC-жалоб", complaint_prompt)
        self.assertIn("не подменяй их IC-законами", complaint_prompt)


if __name__ == "__main__":
    unittest.main()
