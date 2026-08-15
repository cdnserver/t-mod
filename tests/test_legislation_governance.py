import tempfile
import unittest
from pathlib import Path

import storage

from persistence import bill_workspace_repository as workspaces
from persistence import legislation_repository as legislation
from persistence import ovr_repository as ovr


class LegislationGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "governance.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_return_for_changes_preserves_history_and_reopens_same_workspace(self) -> None:
        workspace, _ = workspaces.create_or_get_bill_workspace(
            guild_id=1, author_id=2, author_display="Автор", parent_channel_id=3
        )
        workspace = workspaces.update_bill_workspace(
            workspace["id"],
            expected_revision=workspace["revision"],
            title="Проект для проверки",
            summary="Полный текст проекта, который должен пройти модерацию.",
            status="review",
        )
        workspace = legislation.submit_for_moderation(
            workspace["id"], guild_id=1, author_id=2,
            author_display="Автор", expected_revision=workspace["revision"]
        )
        workspace = legislation.record_moderation_decision(
            workspace["id"], guild_id=1, moderator_id=9,
            moderator_display="Председатель", expected_revision=workspace["revision"],
            decision="changes_requested", note="Добавьте порядок исполнения."
        )
        reopened, created = workspaces.create_or_get_bill_workspace(
            guild_id=1, author_id=2, author_display="Автор", parent_channel_id=3
        )
        self.assertFalse(created)
        self.assertEqual(reopened["id"], workspace["id"])
        self.assertEqual(len(legislation.moderation_events([workspace["id"]])[workspace["id"]]), 2)

    def test_ovr_private_fields_are_visible_only_to_authorized_desk(self) -> None:
        case = ovr.create_case(
            guild_id=1, first_name="Saul", last_name="Goodman", static_id="123",
            discord_text="saul", discord_user_id=None,
            forum_url="https://example.org/profile", additional_info="Знакомый участника",
            actor_id=2, actor_display="Инициатор",
        )
        case = ovr.update_case(
            case["id"], guild_id=1, expected_revision=case["revision"],
            action="update", actor_id=9, actor_display="ОВР",
            findings="Служебные сведения", nowa_links="Связь проверяется", risk_level="medium",
        )
        public = ovr.list_cases(1, actor_id=2, full_access=False)[0]
        private = ovr.list_cases(1, actor_id=9, full_access=True)[0]
        self.assertNotIn("findings", public)
        self.assertNotIn("nowa_links", public)
        self.assertEqual(private["findings"], "Служебные сведения")


if __name__ == "__main__":
    unittest.main()
