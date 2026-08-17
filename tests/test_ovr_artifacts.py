import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from modules.ovr_artifacts import (
    encrypt_investigation_report,
    generate_investigation_report,
)


def _detail() -> dict:
    return {
        "case": {
            "id": 12,
            "guild_id": 77,
            "case_number": 14,
            "revision": 8,
            "first_name": "Зигмунд",
            "last_name": "Правосудов",
            "static_id": "263345",
            "discord_text": "zigmund",
            "forum_url": "https://forum.example.test/people/263345",
            "case_kind": "admission",
            "status": "decision",
            "risk_level": "medium",
            "priority": "important",
            "classification": "secret",
            "assigned_to_display": "Сотрудник ОВР",
            "created_by_display": "Секретарь Товарищества",
            "created_at": "2026-08-16T13:40:00+00:00",
            "due_at": "2026-08-18T13:40:00+00:00",
            "objective": "Установить биографию кандидата, подтверждённые связи и факторы риска до принятия решения о допуске.",
            "additional_info": "Проверка открыта после обращения кандидата о вступлении в Товарищество.",
            "aliases": "Зиг, Правосудов",
            "affiliations": "Phoenix, государственная организация",
            "hypothesis": "Кандидат не имеет скрытых конфликтов интересов; версия требует проверки по независимым источникам.",
            "executive_summary": "Идентификация подтверждена. Собраны два независимых материала и карта ближайших связей.",
            "findings": "Форумный профиль и сведения из обращения совпадают. Противоречий в установленных фактах не обнаружено.",
            "nowa_links": "Подтверждённых связей с семьёй Nowa не установлено.",
            "decision": "approved",
            "decision_reason": "Достаточная полнота сведений и отсутствие подтверждённых критических рисков.",
        },
        "materials": [
            {
                "kind": "document",
                "status": "verified",
                "reliability": "confirmed",
                "title": "Анкета кандидата",
                "source_url": "https://example.test/form",
                "content": "Имя, статик, Discord и форумный профиль подтверждены.",
                "created_by_display": "Сотрудник ОВР",
                "created_at": "2026-08-16T14:00:00+00:00",
            },
            {
                "kind": "observation",
                "status": "verified",
                "reliability": "high",
                "title": "Перекрёстная проверка",
                "source_url": "",
                "content": "Ключевые сведения подтверждены вторым сотрудником.",
                "created_by_display": "Аналитик ОВР",
                "created_at": "2026-08-17T09:15:00+00:00",
            },
        ],
        "relations": [
            {
                "person_name": "Свидетель № 1",
                "relation_type": "Рекомендатель",
                "static_id": "8123",
                "discord_text": "witness",
                "confidence": "confirmed",
                "details": "Личность и характер связи подтверждены.",
            }
        ],
        "tasks": [
            {
                "title": "Проверить форумный профиль",
                "description": "Сверить публикации и указанные биографические сведения.",
                "status": "done",
                "priority": "important",
                "assignee_display": "Аналитик ОВР",
                "due_at": "2026-08-17T18:00:00+00:00",
            }
        ],
        "events": [
            {
                "action": "created",
                "actor_display": "Секретарь Товарищества",
                "note": "Зарегистрировано обращение кандидата.",
                "created_at": "2026-08-16T13:40:00+00:00",
            },
            {
                "action": "claim",
                "actor_display": "Сотрудник ОВР",
                "note": "Расследование принято в работу.",
                "created_at": "2026-08-16T13:45:00+00:00",
            },
            {
                "action": "decision",
                "actor_display": "Руководитель ОВР",
                "note": "Материалы готовы к вынесению решения.",
                "created_at": "2026-08-18T11:10:00+00:00",
            },
        ],
    }


class OvrArtifactTests(unittest.TestCase):
    def test_report_is_complete_and_password_protected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "plain.pdf"
            protected = Path(directory) / "protected.pdf"
            generate_investigation_report(_detail(), destination=plain, force=True)
            encrypt_investigation_report(plain, protected, "OVR-test-2026")

            reader = PdfReader(str(protected))
            self.assertTrue(reader.is_encrypted)
            self.assertEqual(int(reader.decrypt("incorrect-password")), 0)
            self.assertGreater(int(reader.decrypt("OVR-test-2026")), 0)
            self.assertGreaterEqual(len(reader.pages), 4)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("ОВР-014", text)
            self.assertIn("Зигмунд Правосудов", text)
            self.assertIn("Неизменяемая хронология", text)

    def test_short_password_is_rejected_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "plain.pdf"
            protected = Path(directory) / "protected.pdf"
            generate_investigation_report(_detail(), destination=plain, force=True)
            with self.assertRaisesRegex(ValueError, "ovr_report_password_invalid"):
                encrypt_investigation_report(plain, protected, "short")
            self.assertFalse(protected.exists())


if __name__ == "__main__":
    unittest.main()
