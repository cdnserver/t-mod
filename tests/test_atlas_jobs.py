import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage
from persistence import atlas_job_repository, atlas_repository


class AtlasJobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-jobs-test.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        self.organization_id = int(dashboard["organization"]["id"])

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_enqueue_is_idempotent_and_workspace_authorized(self) -> None:
        first = atlas_job_repository.atlas_job_enqueue(
            self.organization_id,
            42,
            job_type="atlas.knowledge.index.v1",
            dedupe_key="source:1:checksum",
            payload={"source_id": 1},
            subject_type="knowledge_source",
            subject_id=1,
        )
        repeated = atlas_job_repository.atlas_job_enqueue(
            self.organization_id,
            42,
            job_type="atlas.knowledge.index.v1",
            dedupe_key="source:1:checksum",
            payload={"source_id": 999},
        )

        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(repeated["payload"], {"source_id": 1})
        self.assertEqual(len(atlas_job_repository.atlas_jobs(self.organization_id)), 1)
        with self.assertRaisesRegex(ValueError, "atlas_job_forbidden"):
            atlas_job_repository.atlas_job_enqueue(
                self.organization_id,
                999,
                job_type="atlas.knowledge.index.v1",
                dedupe_key="forbidden",
            )

    def test_lease_fences_stale_worker_and_persists_progress(self) -> None:
        queued = atlas_job_repository.atlas_job_enqueue(
            self.organization_id,
            42,
            job_type="atlas.knowledge.index.v1",
            dedupe_key="source:2",
        )
        claimed = atlas_job_repository.atlas_job_claim(
            worker_id="worker-a",
            job_types=("atlas.knowledge.index.v1",),
            lease_seconds=60,
        )[0]

        self.assertEqual(claimed["id"], queued["id"])
        self.assertEqual(claimed["attempts"], 1)
        self.assertTrue(
            atlas_job_repository.atlas_job_progress(
                int(claimed["id"]),
                lease_token=str(claimed["lease_token"]),
                progress={"percent": 45, "stage": "embedding"},
            )
        )
        self.assertFalse(
            atlas_job_repository.atlas_job_succeed(
                int(claimed["id"]),
                lease_token="stale-token",
            )
        )
        self.assertTrue(
            atlas_job_repository.atlas_job_succeed(
                int(claimed["id"]),
                lease_token=str(claimed["lease_token"]),
                result={"points": 4},
            )
        )
        stored = atlas_job_repository.atlas_job_get(int(claimed["id"]))
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["progress"], {"percent": 100})
        self.assertEqual(stored["result"], {"points": 4})

    def test_retry_survives_worker_failure_and_honours_attempt_limit(self) -> None:
        start = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        queued = atlas_job_repository.atlas_job_enqueue(
            self.organization_id,
            42,
            job_type="atlas.knowledge.index.v1",
            dedupe_key="source:3",
            max_attempts=2,
            available_at=start.isoformat(),
        )
        first = atlas_job_repository.atlas_job_claim(
            worker_id="worker-a",
            now=start.isoformat(),
            lease_seconds=30,
        )[0]
        status = atlas_job_repository.atlas_job_fail(
            int(first["id"]),
            lease_token=str(first["lease_token"]),
            error="temporary outage",
            retry_delay_seconds=10,
            now=start.isoformat(),
        )
        self.assertEqual(status, "retry")
        self.assertEqual(
            atlas_job_repository.atlas_job_claim(
                worker_id="worker-b",
                now=(start + timedelta(seconds=5)).isoformat(),
            ),
            [],
        )
        second = atlas_job_repository.atlas_job_claim(
            worker_id="worker-b",
            now=(start + timedelta(seconds=11)).isoformat(),
        )[0]
        status = atlas_job_repository.atlas_job_fail(
            int(second["id"]),
            lease_token=str(second["lease_token"]),
            error="still unavailable",
            now=(start + timedelta(seconds=12)).isoformat(),
        )
        self.assertEqual(status, "failed")
        self.assertEqual(
            atlas_job_repository.atlas_job_get(int(queued["id"]))["attempts"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
