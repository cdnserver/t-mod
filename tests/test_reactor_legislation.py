import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage

from modules.bill_editor_ai import BillEditorDraft
from modules.consensus_runtime import active_sessions
from modules.reactor_legislation import (
    ReactorLegislationError,
    cancel_workspace,
    create_workspace,
    generate_workspace_draft,
    legislation_snapshot,
    publish_workspace,
    save_workspace,
)
from persistence import bill_workspace_repository as workspace_storage


class ReactorLegislationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "reactor-legislation-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    @staticmethod
    def complete_payload(workspace: dict) -> dict:
        return {
            "workspace_id": workspace["id"],
            "expected_revision": workspace["revision"],
            "idea": "Создать единый открытый справочник участников.",
            "desired_outcome": "Участники смогут быстрее находить друг друга.",
            "constraints_text": "Не публиковать закрытые данные.",
            "title": "О справочнике участников",
            "summary": (
                "Создать единый справочник участников Товарищества с кратким "
                "описанием их направлений деятельности."
            ),
            "materials": "",
            "implementation_plan": "Подготовить форму и открыть справочник.",
            "leadership_actions": "Назначить ответственного за актуальность данных.",
        }

    def test_workspace_projection_is_owned_durable_and_public_safe(self) -> None:
        workspace, created = create_workspace(77, 101, "Автор")
        self.assertTrue(created)
        self.assertEqual(workspace["progress"], 0)
        self.assertNotIn("parent_channel_id", workspace)
        self.assertNotIn("panel_message_id", workspace)

        saved = save_workspace(77, 101, self.complete_payload(workspace))

        self.assertTrue(saved["ready"])
        self.assertEqual(saved["decision_category"], "ordinary")
        reopened, created_again = create_workspace(77, 101, "Автор")
        self.assertFalse(created_again)
        self.assertEqual(reopened["id"], saved["id"])
        with self.assertRaises(ReactorLegislationError):
            save_workspace(77, 202, self.complete_payload(saved))

    def test_ai_generation_updates_same_revisioned_workspace(self) -> None:
        workspace, _ = create_workspace(77, 101, "Автор")
        payload = self.complete_payload(workspace)
        payload.update({"title": "", "summary": "", "implementation_plan": "", "leadership_actions": ""})
        generated = BillEditorDraft(
            title="О справочнике участников",
            summary="Создать единый справочник участников Товарищества для знакомства и координации.",
            materials=None,
            decision_category="ordinary",
            implementation_plan="Подготовить форму и открыть справочник.",
            leadership_actions="Назначить ответственного за актуальность.",
            clarification="Уточнить перечень публичных полей.",
        )

        with patch(
            "modules.reactor_legislation.generate_bill_editor_draft",
            return_value=generated,
        ):
            updated, clarification = generate_workspace_draft(77, 101, payload)

        self.assertEqual(updated["title"], generated.title)
        self.assertEqual(updated["ai_revision"], 1)
        self.assertGreater(updated["revision"], workspace["revision"])
        self.assertEqual(clarification, generated.clarification)

    def test_invalid_ids_lengths_and_stale_revisions_return_domain_errors(self) -> None:
        workspace, _ = create_workspace(77, 101, "Автор")
        payload = self.complete_payload(workspace)
        saved = save_workspace(77, 101, payload)

        with self.assertRaises(ReactorLegislationError) as stale:
            save_workspace(77, 101, payload)
        self.assertEqual(stale.exception.code, "bill_workspace_revision_conflict")

        oversized = self.complete_payload(saved)
        oversized["idea"] = "я" * 1801
        with self.assertRaises(ReactorLegislationError) as too_long:
            save_workspace(77, 101, oversized)
        self.assertEqual(too_long.exception.code, "bill_field_too_long")

        with self.assertRaises(ReactorLegislationError) as invalid:
            cancel_workspace(
                77,
                101,
                {"workspace_id": "not-a-number", "confirmed": True},
            )
        self.assertEqual(invalid.exception.code, "bill_workspace_id_invalid")

    async def test_incomplete_and_active_consensus_publication_are_blocked(self) -> None:
        workspace, _ = create_workspace(77, 101, "Автор")
        bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        with patch.dict(active_sessions, {}, clear=True):
            with self.assertRaises(ReactorLegislationError) as incomplete:
                await publish_workspace(
                    bot,
                    77,
                    101,
                    "Автор",
                    {
                        "workspace_id": workspace["id"],
                        "expected_revision": workspace["revision"],
                        "confirmed": True,
                    },
                )
        self.assertEqual(incomplete.exception.code, "bill_workspace_incomplete")

        saved = save_workspace(77, 101, self.complete_payload(workspace))
        with patch.dict(
            active_sessions,
            {77: SimpleNamespace(finished=False)},
            clear=True,
        ):
            with self.assertRaises(ReactorLegislationError) as active:
                await publish_workspace(
                    bot,
                    77,
                    101,
                    "Автор",
                    {
                        "workspace_id": saved["id"],
                        "expected_revision": saved["revision"],
                        "confirmed": True,
                    },
                )
        self.assertEqual(
            active.exception.code,
            "bill_submission_locked_by_active_consensus",
        )

    async def test_publication_is_outbox_backed_and_network_retry_is_idempotent(self) -> None:
        workspace, _ = create_workspace(77, 101, "Автор")
        saved = save_workspace(77, 101, self.complete_payload(workspace))
        payload = {
            "workspace_id": saved["id"],
            "expected_revision": saved["revision"],
            "confirmed": True,
        }
        bot = SimpleNamespace(get_guild=lambda _guild_id: None)

        with (
            patch.dict(active_sessions, {}, clear=True),
            patch(
                "modules.reactor_legislation.refresh_bill_workspace_panel",
                AsyncMock(),
            ),
            patch("modules.reactor_legislation.wake_delivery_worker"),
        ):
            bill, created = await publish_workspace(bot, 77, 101, "Автор", payload)
            retried, created_again = await publish_workspace(bot, 77, 101, "Автор", payload)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(retried["id"], bill["id"])
        self.assertEqual(len(legislation_snapshot(77, 101)["bills"]), 1)
        closed = workspace_storage.get_bill_workspace(saved["id"])
        self.assertEqual(closed["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
