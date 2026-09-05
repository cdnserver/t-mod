import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from modules.atlas_media import (
    AtlasLocalBlobStore,
    AtlasMediaConfig,
    AtlasMediaError,
    atlas_media_detect_type,
)
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import atlas_job_repository, atlas_media_repository, atlas_repository
from persistence.core import connect


class AtlasMediaRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name) / "data"
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-media-test.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Владелец")
        self.organization_id = int(dashboard["organization"]["id"])
        with connect() as con:
            con.execute(
                """
                INSERT INTO atlas_memberships(
                    organization_id, guild_id, user_id, display_name, role, status,
                    created_at, updated_at
                ) VALUES(?, 77, 84, 'Участник', 'member', 'active', ?, ?)
                """,
                (self.organization_id, "2026-08-22T00:00:00+00:00", "2026-08-22T00:00:00+00:00"),
            )
            con.commit()
        self.store = AtlasLocalBlobStore(Path(self.temp_dir.name) / "blobs")

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_resumable_upload_is_idempotent_private_and_links_to_timeline(self) -> None:
        content = "Проверенный материал Atlas Media".encode("utf-8")
        upload = atlas_media_repository.atlas_media_begin_upload(
            self.organization_id,
            42,
            title="Материал инцидента",
            filename="incident.txt",
            size_bytes=len(content),
            declared_mime_type="text/plain",
            client_request_id="media-request-1",
            max_asset_bytes=1024,
            quota_bytes=4096,
        )
        repeated = atlas_media_repository.atlas_media_begin_upload(
            self.organization_id,
            42,
            title="Повтор",
            filename="repeat.txt",
            size_bytes=len(content),
            client_request_id="media-request-1",
            max_asset_bytes=1024,
            quota_bytes=4096,
        )
        self.assertEqual(upload["upload_id"], repeated["upload_id"])

        first = content[:10]
        received = self.store.append_chunk(upload["temp_storage_key"], offset=0, data=first)
        self.assertEqual(
            self.store.append_chunk(upload["temp_storage_key"], offset=0, data=first),
            received,
        )
        atlas_media_repository.atlas_media_record_upload_progress(
            upload["upload_id"], expected_offset=0, received_size=received
        )
        received = self.store.append_chunk(
            upload["temp_storage_key"], offset=received, data=content[received:]
        )
        atlas_media_repository.atlas_media_record_upload_progress(
            upload["upload_id"], expected_offset=10, received_size=received
        )
        checksum, size = self.store.checksum_and_size(upload["temp_storage_key"])
        mime_type = atlas_media_detect_type(self.store.head(upload["temp_storage_key"]), "text/plain")
        final_key = self.store.final_key(checksum)
        atlas_media_repository.atlas_media_prepare_finalize(
            upload["upload_id"],
            checksum_sha256=checksum,
            final_storage_key=final_key,
            detected_mime_type=mime_type,
        )
        self.store.finalize(upload["temp_storage_key"], final_key, expected_size=size)
        asset = atlas_media_repository.atlas_media_complete_upload(
            upload["upload_id"],
            checksum_sha256=checksum,
            storage_key=final_key,
            mime_type=mime_type,
            media_kind="file",
            scan_status="not_configured",
        )

        self.assertEqual(asset["status"], "ready")
        self.assertEqual(asset["blob_checksum"], hashlib.sha256(content).hexdigest())
        self.assertEqual(len(atlas_media_repository.atlas_media_assets(self.organization_id, 42)), 1)
        self.assertEqual(atlas_media_repository.atlas_media_assets(self.organization_id, 84), [])
        timeline = atlas_repository.atlas_timeline_events(self.organization_id)
        self.assertEqual(timeline[0]["source_type"], "media_asset")
        event = atlas_repository.atlas_create_timeline_event(
            self.organization_id, 42, title="Связанный инцидент"
        )
        link = atlas_repository.atlas_link_entities(
            self.organization_id,
            42,
            source_type="timeline_event",
            source_id=event["id"],
            relation="supports",
            target_type="media_asset",
            target_id=asset["id"],
        )
        self.assertEqual(link["target_type"], "media_asset")
        with self.assertRaisesRegex(ValueError, "atlas_entity_target_not_found"):
            atlas_repository.atlas_link_entities(
                self.organization_id,
                84,
                source_type="timeline_event",
                source_id=event["id"],
                relation="supports",
                target_type="media_asset",
                target_id=asset["id"],
            )

    def test_store_rejects_path_escape_and_executable(self) -> None:
        with self.assertRaisesRegex(AtlasMediaError, "Недопустимый путь"):
            self.store.path("../../secret")
        with self.assertRaisesRegex(AtlasMediaError, "Исполняемые"):
            atlas_media_detect_type(b"MZ" + b"\0" * 100, "application/octet-stream")


class AtlasMediaWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_survives_as_job_and_content_is_downloadable(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name) / "data"
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-media-web.db"
        storage.init_db()
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="media-csrf",
            member=SimpleNamespace(
                id=42,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        config = AtlasMediaConfig(
            root=Path(temp_dir.name) / "media",
            max_asset_bytes=1024 * 1024,
            quota_bytes=8 * 1024 * 1024,
            max_chunk_bytes=16,
            scan_command="",
        )
        app = web.Application(client_max_size=1024 * 1024)
        with patch("modules.atlas_web.AtlasMediaConfig.from_env", return_value=config):
            register_atlas_web_routes(
                app,
                SimpleNamespace(get_guild=lambda guild_id: None),
                guild_id=77,
                asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
                authenticate=authenticate,
            )
        content = "Atlas сохраняет файлы после перезапуска".encode("utf-8")
        headers = {"X-CSRF-Token": "media-csrf", "X-Idempotency-Key": "web-upload-1"}
        try:
            async with TestClient(TestServer(app)) as client:
                started = await client.post(
                    "/api/atlas/media/uploads",
                    json={
                        "title": "Проверка загрузки",
                        "filename": "proof.txt",
                        "size_bytes": len(content),
                        "mime_type": "text/plain",
                        "visibility_scope": "private",
                    },
                    headers=headers,
                )
                start_payload = await started.json()
                self.assertEqual(started.status, 201, start_payload)
                upload_url = start_payload["upload_url"]
                offset = 0
                final_payload = None
                while offset < len(content):
                    chunk = content[offset : offset + 16]
                    response = await client.put(
                        upload_url,
                        data=chunk,
                        headers={"X-CSRF-Token": "media-csrf", "Upload-Offset": str(offset)},
                    )
                    final_payload = await response.json()
                    self.assertIn(response.status, {200, 202}, final_payload)
                    offset = int(response.headers["Upload-Offset"])
                job_id = int(final_payload["job"]["id"])
                for _attempt in range(30):
                    status = await client.get(f"/api/atlas/jobs/{job_id}")
                    job = (await status.json())["job"]
                    if job["status"] not in {"pending", "running", "retry"}:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(job["status"], "succeeded", job)
                library = await client.get("/api/atlas/media")
                library_payload = await library.json()
                self.assertEqual(len(library_payload["items"]), 1)
                asset = library_payload["items"][0]
                self.assertEqual(asset["status"], "ready")
                self.assertNotIn("storage_key", asset)
                downloaded = await client.get(asset["content_url"])
                self.assertEqual(await downloaded.read(), content)
                self.assertEqual(downloaded.headers["X-Content-Type-Options"], "nosniff")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()

    async def test_pending_finalize_resumes_on_new_web_runtime(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name) / "data"
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-media-restart.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Владелец")
        organization_id = int(dashboard["organization"]["id"])
        content = b"Atlas durable media upload after restart"
        config = AtlasMediaConfig(
            root=Path(temp_dir.name) / "media",
            max_asset_bytes=1024 * 1024,
            quota_bytes=8 * 1024 * 1024,
            max_chunk_bytes=1024,
            scan_command="",
        )
        store = AtlasLocalBlobStore(config.root)
        upload = atlas_media_repository.atlas_media_begin_upload(
            organization_id,
            42,
            title="После перезапуска",
            filename="restart.txt",
            size_bytes=len(content),
            declared_mime_type="text/plain",
            client_request_id="restart-upload",
            max_asset_bytes=config.max_asset_bytes,
            quota_bytes=config.quota_bytes,
        )
        received = store.append_chunk(upload["temp_storage_key"], offset=0, data=content)
        atlas_media_repository.atlas_media_record_upload_progress(
            upload["upload_id"], expected_offset=0, received_size=received
        )
        job = atlas_job_repository.atlas_job_enqueue(
            organization_id,
            42,
            job_type="atlas.media.finalize.v1",
            dedupe_key=f"upload:{upload['upload_id']}",
            payload={"upload_id": upload["upload_id"]},
            subject_type="media_asset",
            subject_id=upload["asset_id"],
        )

        async def authenticate(_request):
            return None, False

        app = web.Application()
        try:
            with patch("modules.atlas_web.AtlasMediaConfig.from_env", return_value=config):
                register_atlas_web_routes(
                    app,
                    SimpleNamespace(get_guild=lambda guild_id: None),
                    guild_id=77,
                    asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
                    authenticate=authenticate,
                )
            async with TestClient(TestServer(app)):
                for _attempt in range(40):
                    stored = atlas_job_repository.atlas_job_get(int(job["id"]))
                    if stored["status"] not in {"pending", "running", "retry"}:
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual(stored["status"], "succeeded", stored)
            asset = atlas_media_repository.atlas_media_asset(
                organization_id, 42, int(upload["asset_id"])
            )
            self.assertEqual(asset["status"], "ready")
            self.assertEqual(store.path(str(asset["storage_key"])).read_bytes(), content)
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()


class AtlasLocalBlobStorePersistenceTests(unittest.TestCase):
    def test_put_bytes_is_content_addressed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtlasLocalBlobStore(Path(directory))
            payload = b"permanent forum image"
            digest = hashlib.sha256(payload).hexdigest()

            first = store.put_bytes(payload, checksum_sha256=digest)
            second = store.put_bytes(payload, checksum_sha256=digest)

            self.assertEqual(first, second)
            self.assertEqual(store.path(first).read_bytes(), payload)

    def test_put_bytes_rejects_wrong_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtlasLocalBlobStore(Path(directory))
            with self.assertRaisesRegex(AtlasMediaError, "Контрольная сумма"):
                store.put_bytes(b"image", checksum_sha256="0" * 64)


if __name__ == "__main__":
    unittest.main()
