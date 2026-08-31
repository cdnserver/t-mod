import csv
import json
import tempfile
import unittest
from pathlib import Path

from modules.atlas_finetuning import (
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
) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "thread_id": thread_id,
        "agent_id": "atlas-tvr-a",
        "user_text": prompt,
        "assistant_text": answer,
        "model": "test-model",
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

    def test_deduplicates_and_marks_low_value_answers_for_review(self) -> None:
        rows = [
            candidate(1, 10, "Что такое УК?", "Этой информации нет в библиотеке Atlas."),
            candidate(2, 11, "Что такое УК?", "Этой информации нет в библиотеке Atlas."),
        ]
        prepared, rejected = prepare_candidates(rows)
        self.assertEqual(len(prepared), 1)
        self.assertIn("low_value_review", prepared[0].flags)
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


if __name__ == "__main__":
    unittest.main()
