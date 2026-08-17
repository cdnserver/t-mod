import sqlite3
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

    def test_ovr_investigation_lifecycle_is_atomic_and_revision_safe(self) -> None:
        case = ovr.create_case(
            guild_id=1, first_name="Jimmy", last_name="McGill", static_id="456",
            discord_text="jimmy", discord_user_id=None, forum_url=None,
            additional_info="Кандидат на вступление", actor_id=2,
            actor_display="Инициатор", case_kind="admission", priority="important",
            objective="Проверить биографию и значимые связи.",
        )
        stale_revision = case["revision"]
        case = ovr.update_case(
            case["id"], guild_id=1, expected_revision=case["revision"],
            action="claim", actor_id=9, actor_display="Сотрудник ОВР",
        )
        with self.assertRaisesRegex(ValueError, "ovr_case_revision_conflict"):
            ovr.add_material(
                case["id"], guild_id=1, expected_revision=stale_revision,
                actor_id=9, actor_display="Сотрудник ОВР", kind="document",
                title="Устаревшая запись", content="Не должна сохраниться.",
            )

        detail = ovr.add_material(
            case["id"], guild_id=1, expected_revision=case["revision"],
            actor_id=9, actor_display="Сотрудник ОВР", kind="document",
            title="Профиль кандидата", content="Сведения подтверждены источником.",
            reliability="high",
        )
        detail = ovr.set_material_status(
            case["id"], guild_id=1,
            expected_revision=detail["case"]["revision"],
            material_id=detail["materials"][0]["id"], status="verified",
            actor_id=9, actor_display="Сотрудник ОВР",
        )
        detail = ovr.add_relation(
            case["id"], guild_id=1,
            expected_revision=detail["case"]["revision"],
            actor_id=9, actor_display="Сотрудник ОВР", person_name="Kim Wexler",
            relation_type="Доверенное лицо", confidence="confirmed",
        )
        detail = ovr.add_task(
            case["id"], guild_id=1,
            expected_revision=detail["case"]["revision"],
            actor_id=9, actor_display="Сотрудник ОВР", title="Проверить форум",
            assignee_display="Аналитик", priority="urgent",
        )
        detail = ovr.set_task_status(
            case["id"], guild_id=1,
            expected_revision=detail["case"]["revision"],
            task_id=detail["tasks"][0]["id"], status="done",
            actor_id=9, actor_display="Сотрудник ОВР",
        )
        case = ovr.update_case(
            case["id"], guild_id=1,
            expected_revision=detail["case"]["revision"], action="analysis",
            actor_id=9, actor_display="Сотрудник ОВР", hypothesis="Риски не выявлены",
            executive_summary="Материалы проверены.", risk_level="low",
        )
        case = ovr.update_case(
            case["id"], guild_id=1, expected_revision=case["revision"],
            action="decision", actor_id=9, actor_display="Сотрудник ОВР",
        )
        case = ovr.update_case(
            case["id"], guild_id=1, expected_revision=case["revision"],
            action="approve", actor_id=9, actor_display="Сотрудник ОВР",
            note="Проверка завершена, препятствий не установлено.",
        )
        final = ovr.case_detail(case["id"], guild_id=1)
        self.assertEqual(final["case"]["status"], "approved")
        self.assertEqual(final["case"]["progress"], 100)
        self.assertEqual(len(final["materials"]), 1)
        self.assertEqual(final["materials"][0]["status"], "verified")
        self.assertEqual(len(final["relations"]), 1)
        self.assertEqual(final["tasks"][0]["status"], "done")
        self.assertGreaterEqual(len(final["events"]), 9)

        reopened = ovr.update_case(
            case["id"], guild_id=1, expected_revision=case["revision"],
            action="reopen", actor_id=9, actor_display="Сотрудник ОВР",
            note="Появились новые обстоятельства.",
        )
        self.assertEqual(reopened["status"], "screening")

    def test_ovr_migration_preserves_existing_cases(self) -> None:
        storage.DATABASE_FILE.unlink()
        with sqlite3.connect(storage.DATABASE_FILE) as con:
            con.executescript(
                """
                CREATE TABLE ovr_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    case_number INTEGER NOT NULL,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    static_id TEXT NOT NULL,
                    discord_text TEXT NOT NULL,
                    discord_user_id INTEGER,
                    forum_url TEXT,
                    additional_info TEXT,
                    nowa_links TEXT,
                    findings TEXT,
                    risk_level TEXT NOT NULL DEFAULT 'unrated',
                    status TEXT NOT NULL DEFAULT 'new',
                    decision TEXT,
                    decision_reason TEXT,
                    assigned_to_id INTEGER,
                    assigned_to_display TEXT,
                    created_by_id INTEGER NOT NULL,
                    created_by_display TEXT,
                    due_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    decided_at TEXT,
                    UNIQUE(guild_id, case_number)
                );
                INSERT INTO ovr_cases(
                    guild_id, case_number, first_name, last_name, static_id,
                    discord_text, created_by_id, due_at, created_at, updated_at
                ) VALUES(
                    1, 12, 'Старое', 'Дело', '777', 'legacy', 2,
                    '2026-08-20T00:00:00+00:00',
                    '2026-08-18T00:00:00+00:00',
                    '2026-08-18T00:00:00+00:00'
                );
                """
            )

        storage.init_db()
        cases = ovr.list_cases(1, full_access=True)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["case_number"], 12)
        self.assertEqual(cases[0]["case_kind"], "admission")
        self.assertEqual(cases[0]["priority"], "normal")
        detail = ovr.case_detail(cases[0]["id"], guild_id=1)
        self.assertEqual(detail["materials"], [])
        self.assertEqual(detail["relations"], [])
        self.assertEqual(detail["tasks"], [])


if __name__ == "__main__":
    unittest.main()
