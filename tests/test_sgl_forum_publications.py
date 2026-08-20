import gc
import tempfile
import unittest
from pathlib import Path

from persistence import core, schema, sgl_repository


class SGLForumPublicationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_data_dir = core.DATA_DIR
        self.previous_database_file = core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "sgl-forum-publications.db"
        schema.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=17,
            client_id=101,
            client_display="Client",
            lead_lawyer_id=202,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=202,
            created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None

    def tearDown(self) -> None:
        core.DATA_DIR = self.previous_data_dir
        core.DATABASE_FILE = self.previous_database_file
        gc.collect()
        self.temp_dir.cleanup()

    def _draft(self):
        return sgl_repository.create_sgl_case_forum_publication(
            case=self.case,
            target_url="https://forum.majestic-rp.ru/forums/court.42/",
            title="Исковое заявление · SGL №001",
            body="[B]Обстоятельства[/B]\nПроверенный текст иска.",
            created_by_id=202,
            created_by_display="Lawyer",
        )

    def test_draft_is_editable_then_claimed_once_and_published(self) -> None:
        draft = self._draft()
        self.assertEqual(draft["status"], "draft")
        edited = sgl_repository.update_sgl_case_forum_publication(
            guild_id=17,
            case_number=self.case.case_number,
            publication_id=draft["id"],
            target_url=draft["target_url"],
            title="Уточнённый иск",
            body="Уточнённый текст",
            actor_id=202,
            actor_display="Lawyer",
            expected_updated_at=draft["updated_at"],
        )
        assert edited is not None
        self.assertEqual(edited["title"], "Уточнённый иск")
        claimed = sgl_repository.claim_sgl_case_forum_publication(
            guild_id=17,
            case_number=self.case.case_number,
            publication_id=draft["id"],
            actor_id=202,
            actor_display="Lawyer",
            expected_updated_at=edited["updated_at"],
        )
        assert claimed is not None
        self.assertEqual(claimed["status"], "publishing")
        self.assertEqual(claimed["attempts"], 1)
        with self.assertRaisesRegex(ValueError, "sgl_forum_publication_busy"):
            sgl_repository.claim_sgl_case_forum_publication(
                guild_id=17,
                case_number=self.case.case_number,
                publication_id=draft["id"],
                actor_id=202,
                actor_display="Lawyer",
            )
        published = sgl_repository.mark_sgl_case_forum_publication_published(
            publication_id=draft["id"],
            forum_url="https://forum.majestic-rp.ru/threads/claim.999/",
        )
        assert published is not None
        self.assertEqual(published["status"], "published")
        updated_case = sgl_repository.get_sgl_case_by_number(17, self.case.case_number)
        assert updated_case is not None
        self.assertEqual(updated_case.claim_link, published["forum_url"])

    def test_failed_attempt_can_be_revised_and_retried(self) -> None:
        draft = self._draft()
        claimed = sgl_repository.claim_sgl_case_forum_publication(
            guild_id=17,
            case_number=self.case.case_number,
            publication_id=draft["id"],
            actor_id=202,
            actor_display="Lawyer",
        )
        assert claimed is not None
        failed = sgl_repository.mark_sgl_case_forum_publication_failed(
            publication_id=draft["id"], error="Forum asks for login",
        )
        assert failed is not None
        self.assertEqual(failed["status"], "failed")
        self.assertIn("login", failed["last_error"])
        retried = sgl_repository.claim_sgl_case_forum_publication(
            guild_id=17,
            case_number=self.case.case_number,
            publication_id=draft["id"],
            actor_id=202,
            actor_display="Lawyer",
        )
        assert retried is not None
        self.assertEqual(retried["status"], "publishing")
        self.assertEqual(retried["attempts"], 2)

    def test_rejects_invalid_external_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "sgl_forum_target_invalid"):
            sgl_repository.create_sgl_case_forum_publication(
                case=self.case,
                target_url="http://forum.majestic-rp.ru/forums/court/",
                title="Иск",
                body="Текст",
                created_by_id=202,
                created_by_display="Lawyer",
            )


if __name__ == "__main__":
    unittest.main()
