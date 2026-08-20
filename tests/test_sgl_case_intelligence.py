import gc
import tempfile
import unittest
from pathlib import Path

from persistence import core, schema, sgl_repository


class SGLCaseIntelligenceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_database_file = core.DATA_DIR, core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "sgl-intelligence.db"
        schema.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=91, client_id=11, client_display="Client",
            lead_lawyer_id=22, lead_lawyer_display="Lawyer",
            secretary_id=None, secretary_display=None,
            created_by_id=22, created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 333)
        assert self.case is not None

    def tearDown(self) -> None:
        core.DATA_DIR, core.DATABASE_FILE = self.old_data_dir, self.old_database_file
        gc.collect()
        self.temp_dir.cleanup()

    def _published_claim(self) -> dict:
        draft = sgl_repository.create_sgl_case_forum_publication(
            case=self.case,
            target_url="https://forum.majestic-rp.ru/forums/court.42/",
            title="Иск · SGL №001", body="Подробные обстоятельства для форума.",
            created_by_id=22, created_by_display="Lawyer",
        )
        claimed = sgl_repository.claim_sgl_case_forum_publication(
            guild_id=91, case_number=self.case.case_number, publication_id=draft["id"],
            actor_id=22, actor_display="Lawyer",
        )
        assert claimed is not None
        published = sgl_repository.mark_sgl_case_forum_publication_published(
            publication_id=draft["id"],
            forum_url="https://forum.majestic-rp.ru/threads/claim.101/",
        )
        assert published is not None
        return published

    def test_atlas_note_is_case_scoped_and_auditable(self) -> None:
        note = sgl_repository.record_sgl_case_ai_note(
            case=self.case, kind="risks", question="Проверь пробелы",
            answer="Не хватает подтверждения даты.", citations=[{"title": "Правило"}],
            agent_id="atlas-claims", response_mode="balanced",
            created_by_id=22, created_by_display="Lawyer",
        )
        self.assertEqual(note["kind"], "risks")
        self.assertEqual(note["citations"][0]["title"], "Правило")
        self.assertEqual(sgl_repository.list_sgl_case_ai_notes(self.case.id)[0]["id"], note["id"])
        self.assertEqual(sgl_repository.get_sgl_case_events(self.case.id, 1)[0]["action"], "atlas_analysis_saved")

    def test_forum_observation_only_alerts_after_the_baseline_changes(self) -> None:
        publication = self._published_claim()
        listed = sgl_repository.list_sgl_forum_publications_for_guild(91)
        self.assertEqual(listed[0]["channel_id"], 333)
        baseline = sgl_repository.observe_sgl_case_forum_publication(
            publication=publication, thread_title="Иск", thread_excerpt="Версия 1", content_fingerprint="a" * 64,
        )
        self.assertEqual(baseline["change_type"], "initial")
        self.assertFalse(baseline["should_notify"])
        unchanged = sgl_repository.observe_sgl_case_forum_publication(
            publication=publication, thread_title="Иск", thread_excerpt="Версия 1", content_fingerprint="a" * 64,
        )
        self.assertEqual(unchanged["change_type"], "unchanged")
        changed = sgl_repository.observe_sgl_case_forum_publication(
            publication=publication, thread_title="Иск", thread_excerpt="Версия 2", content_fingerprint="b" * 64,
        )
        self.assertTrue(changed["should_notify"])
        self.assertEqual(changed["status"], "changed")
        marked = sgl_repository.mark_sgl_forum_observation_notified(changed["id"])
        assert marked is not None
        self.assertEqual(marked["status"], "tracked")
        first_error = sgl_repository.observe_sgl_case_forum_publication(
            publication=publication, error="manual login required",
        )
        self.assertTrue(first_error["should_notify"])
        sgl_repository.mark_sgl_forum_observation_notified(first_error["id"])
        repeat_error = sgl_repository.observe_sgl_case_forum_publication(
            publication=publication, error="manual login required",
        )
        self.assertFalse(repeat_error["should_notify"])


if __name__ == "__main__":
    unittest.main()
