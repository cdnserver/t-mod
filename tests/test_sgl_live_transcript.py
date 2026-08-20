import gc
import tempfile
import unittest
from pathlib import Path

from persistence import core
from persistence import schema
from persistence import sgl_repository


class SGLLiveTranscriptRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_data_dir = core.DATA_DIR
        self.previous_database_file = core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "sgl-live.db"
        schema.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=77,
            client_id=101,
            client_display="Client",
            lead_lawyer_id=202,
            lead_lawyer_display="Lawyer",
            secretary_id=303,
            secretary_display="Secretary",
            created_by_id=202,
            created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None

    def tearDown(self) -> None:
        core.DATA_DIR = self.previous_data_dir
        core.DATABASE_FILE = self.previous_database_file
        # SQLite connections are short-lived in repository methods, but the
        # Windows file handle is released during garbage collection rather
        # than at transaction exit.
        gc.collect()
        self.temp_dir.cleanup()

    def test_web_and_discord_observations_share_one_message(self) -> None:
        web_row = sgl_repository.record_sgl_case_message(
            case=self.case,
            origin="web",
            discord_message_id=800,
            author_id=101,
            author_display="Client",
            author_avatar_url=None,
            author_is_bot=False,
            content="Сообщение из веба",
            attachments=[{"filename": "proof.png", "url": "https://cdn.example/proof.png", "size": 12}],
        )
        observed = sgl_repository.record_sgl_case_message(
            case=self.case,
            origin="discord",
            discord_message_id=800,
            author_id=999,
            author_display="T-Mod",
            author_avatar_url=None,
            author_is_bot=True,
            content="Сообщение из веба",
        )
        self.assertEqual(web_row["id"], observed["id"])
        self.assertEqual(observed["origin"], "web")
        self.assertEqual(observed["attachments"][0]["filename"], "proof.png")
        self.assertEqual(len(sgl_repository.list_sgl_case_messages(self.case.id)), 1)

    def test_discord_edit_and_delete_are_visible_to_the_web_transcript(self) -> None:
        stored = sgl_repository.record_sgl_case_message(
            case=self.case,
            origin="discord",
            discord_message_id=801,
            author_id=101,
            author_display="Client",
            author_avatar_url=None,
            author_is_bot=False,
            content="Первая версия",
        )
        changed = sgl_repository.update_sgl_case_message_from_discord(
            guild_id=77,
            discord_message_id=801,
            content="Исправленная версия",
            attachments=[{"filename": "new.txt", "url": "https://cdn.example/new.txt", "size": "bad"}],
        )
        self.assertEqual(changed["id"], stored["id"])
        self.assertEqual(changed["content"], "Исправленная версия")
        self.assertEqual(changed["attachments"][0]["size"], 0)
        deleted = sgl_repository.mark_sgl_case_message_deleted(
            guild_id=77, discord_message_id=801
        )
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(
            sgl_repository.list_sgl_case_messages(self.case.id)[0]["deleted_at"],
            deleted["deleted_at"],
        )

    def test_messages_are_paginated_in_chronological_order(self) -> None:
        for message_id in (901, 902, 903):
            sgl_repository.record_sgl_case_message(
                case=self.case,
                origin="discord",
                discord_message_id=message_id,
                author_id=101,
                author_display="Client",
                author_avatar_url=None,
                author_is_bot=False,
                content=str(message_id),
            )
        all_rows = sgl_repository.list_sgl_case_messages(self.case.id, limit=2)
        self.assertEqual([item["content"] for item in all_rows], ["902", "903"])
        follow_up = sgl_repository.list_sgl_case_messages(
            self.case.id, after_id=all_rows[0]["id"], limit=10
        )
        self.assertEqual([item["content"] for item in follow_up], ["903"])

    def test_management_update_is_audited_and_conflict_aware(self) -> None:
        changed = sgl_repository.update_sgl_case_management(
            guild_id=77,
            case_number=self.case.case_number,
            values={"request_type": "Иск", "status": "awaiting_link", "secretary_id": None, "secretary_display": None},
            actor_id=202,
            actor_display="Lawyer",
            expected_updated_at=self.case.updated_at,
        )
        assert changed is not None
        self.assertEqual(changed.request_type, "Иск")
        self.assertEqual(changed.status, "awaiting_link")
        self.assertIsNone(changed.secretary_id)
        with self.assertRaisesRegex(ValueError, "sgl_case_revision_conflict"):
            sgl_repository.update_sgl_case_management(
                guild_id=77,
                case_number=self.case.case_number,
                values={"phone": "555"},
                actor_id=202,
                actor_display="Lawyer",
                expected_updated_at=self.case.updated_at,
            )
        actions = [event["action"] for event in sgl_repository.get_sgl_case_events(self.case.id)]
        self.assertIn("web_case_updated", actions)


if __name__ == "__main__":
    unittest.main()
