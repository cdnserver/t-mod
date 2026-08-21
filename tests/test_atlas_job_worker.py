import asyncio
import tempfile
import unittest
from pathlib import Path

import storage
from modules.atlas_jobs import AtlasJobWorker
from persistence import atlas_job_repository, atlas_repository


class AtlasJobWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-worker-test.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        self.organization_id = int(dashboard["organization"]["id"])

    async def asyncTearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_worker_reports_progress_and_completes_job(self) -> None:
        queued = atlas_job_repository.atlas_job_enqueue(
            self.organization_id,
            42,
            job_type="atlas.test.v1",
            dedupe_key="test-1",
            payload={"value": 7},
        )
        calls = []

        async def handler(job, report):
            calls.append(job["payload"]["value"])
            await report({"percent": 50, "stage": "working"})
            return {"answer": 14}

        worker = AtlasJobWorker(worker_id="test-worker", poll_seconds=0.1)
        worker.register("atlas.test.v1", handler)
        processed = await worker.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(calls, [7])
        stored = atlas_job_repository.atlas_job_get(int(queued["id"]))
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["result"], {"answer": 14})

    async def test_started_worker_wakes_for_new_job(self) -> None:
        complete = asyncio.Event()

        async def handler(_job, _report):
            complete.set()
            return {}

        worker = AtlasJobWorker(worker_id="wake-worker", poll_seconds=5)
        worker.register("atlas.test.v1", handler)
        worker.start()
        try:
            atlas_job_repository.atlas_job_enqueue(
                self.organization_id,
                42,
                job_type="atlas.test.v1",
                dedupe_key="test-wake",
            )
            worker.wake()
            await asyncio.wait_for(complete.wait(), timeout=1)
        finally:
            await worker.close()


if __name__ == "__main__":
    unittest.main()
