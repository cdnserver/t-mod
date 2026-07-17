import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules import sgl_archive
from persistence import core
from persistence import schema
from persistence import sgl_archive_repository as archive_repository
from persistence import sgl_repository


class SGLArchiveRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_data_dir = core.DATA_DIR
        self.previous_database_file = core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "test.db"
        schema.init_db()

    def tearDown(self) -> None:
        core.DATA_DIR = self.previous_data_dir
        core.DATABASE_FILE = self.previous_database_file
        self.temp_dir.cleanup()

    def _save_snapshot(self):
        return archive_repository.save_sgl_case_archive_snapshot(
            guild_id=10,
            case_id=None,
            case_number=90,
            original_channel_id=1000,
            original_channel_name="090-closed",
            original_topic="case 090",
            original_category_id=2000,
            metadata={"format_version": 1},
            messages=[
                {
                    "original_message_id": 3000,
                    "container_id": 1000,
                    "container_type": "channel",
                    "container_name": "090-closed",
                    "author_id": 55,
                    "author_name": "user",
                    "author_display": "User",
                    "content": "hello",
                    "embeds": [],
                    "attachments": [],
                    "stickers": [],
                    "reactions": [{"emoji": "✅", "count": 2}],
                    "components": [],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            snapshot_started_at=datetime.now(timezone.utc).isoformat(),
        )

    def test_snapshot_is_atomic_readable_and_sealable(self) -> None:
        archive = self._save_snapshot()
        self.assertEqual(archive.status, "ready")
        self.assertEqual(archive.message_count, 1)
        messages = archive_repository.list_sgl_archive_messages(archive.id)
        self.assertEqual([item.content for item in messages], ["hello"])

        sealed = archive_repository.mark_sgl_archive_source_deleted(archive.id)
        self.assertIsNotNone(sealed)
        assert sealed is not None
        self.assertEqual(sealed.status, "sealed")
        self.assertIsNotNone(sealed.source_deleted_at)
        with self.assertRaisesRegex(ValueError, "already_sealed"):
            self._save_snapshot()

    def test_only_one_active_restoration_is_created(self) -> None:
        archive = self._save_snapshot()
        archive_repository.mark_sgl_archive_source_deleted(archive.id)
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        first = archive_repository.create_sgl_archive_restoration(
            archive_id=archive.id,
            guild_id=10,
            restored_channel_id=4000,
            restored_by_id=60,
            restored_by_display="Staff",
            expires_at=expires,
        )
        second = archive_repository.create_sgl_archive_restoration(
            archive_id=archive.id,
            guild_id=10,
            restored_channel_id=4001,
            restored_by_id=61,
            restored_by_display="Other",
            expires_at=expires,
        )
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.restored_channel_id, 4000)

    def test_sealing_detaches_discord_channel_but_keeps_case_record(self) -> None:
        case = sgl_repository.reserve_sgl_case(
            guild_id=10,
            client_id=70,
            client_display="Client",
            lead_lawyer_id=71,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=72,
            created_by_display="Staff",
        )
        attached = sgl_repository.attach_sgl_case_channel(case.id, 5000)
        self.assertIsNotNone(attached)
        archive = archive_repository.save_sgl_case_archive_snapshot(
            guild_id=10,
            case_id=case.id,
            case_number=case.case_number,
            original_channel_id=5000,
            original_channel_name="001-closed",
            original_topic=None,
            original_category_id=2000,
            metadata={},
            messages=[],
            snapshot_started_at=datetime.now(timezone.utc).isoformat(),
        )
        archive_repository.mark_sgl_archive_source_deleted(archive.id)

        kept = sgl_repository.get_sgl_case_by_number(10, case.case_number)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertIsNone(kept.channel_id)
        actions = [item["action"] for item in sgl_repository.get_sgl_case_events(case.id)]
        self.assertIn("transcript_archived", actions)

    def test_backfill_marker_is_scoped_to_guild_and_category(self) -> None:
        self.assertFalse(archive_repository.is_sgl_archive_backfill_complete(10, 20))
        archive_repository.mark_sgl_archive_backfill_complete(10, 20)
        self.assertTrue(archive_repository.is_sgl_archive_backfill_complete(10, 20))
        self.assertFalse(archive_repository.is_sgl_archive_backfill_complete(10, 21))

    def test_attachment_hash_is_verified_before_source_deletion(self) -> None:
        directory = archive_repository.sgl_archive_case_directory(10, 91)
        attachment = directory / "message-file.txt"
        attachment.write_bytes(b"hello")
        relative = attachment.relative_to(archive_repository.sgl_archive_root()).as_posix()
        archive = archive_repository.save_sgl_case_archive_snapshot(
            guild_id=10,
            case_id=None,
            case_number=91,
            original_channel_id=1001,
            original_channel_name="091-closed",
            original_topic=None,
            original_category_id=2000,
            metadata={},
            messages=[
                {
                    "original_message_id": 3001,
                    "container_id": 1001,
                    "container_type": "channel",
                    "container_name": "091-closed",
                    "author_display": "User",
                    "content": "file",
                    "attachments": [
                        {
                            "filename": "file.txt",
                            "local_path": relative,
                            "size": 5,
                            "sha256": hashlib.sha256(b"hello").hexdigest(),
                        }
                    ],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            snapshot_started_at=datetime.now(timezone.utc).isoformat(),
        )
        asyncio.run(sgl_archive.verify_archive_files(archive))
        attachment.write_bytes(b"jello")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            asyncio.run(sgl_archive.verify_archive_files(archive))


class SGLArchivePolicyTests(unittest.TestCase):
    def test_case_number_parsing_supports_current_channel_names(self) -> None:
        self.assertEqual(sgl_archive.parse_case_number_from_channel("090-✋"), 90)
        self.assertEqual(
            sgl_archive.parse_case_number_from_channel("closed", "SGL case 124"),
            124,
        )
        self.assertIsNone(sgl_archive.parse_case_number_from_channel("archive"))

    def test_retention_is_fourteen_days(self) -> None:
        now = datetime(2026, 7, 17, tzinfo=timezone.utc)
        self.assertTrue(
            sgl_archive.archive_due(
                (now - timedelta(days=14)).isoformat(), now=now
            )
        )
        self.assertFalse(
            sgl_archive.archive_due(
                (now - timedelta(days=13, hours=23)).isoformat(), now=now
            )
        )


class SGLArchiveAttachmentDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_data_dir = core.DATA_DIR
        core.DATA_DIR = Path(self.temp_dir.name)
        self.directory = archive_repository.sgl_archive_case_directory(10, 90)

    async def asyncTearDown(self) -> None:
        core.DATA_DIR = self.previous_data_dir
        self.temp_dir.cleanup()

    @staticmethod
    def _attachment(save: AsyncMock) -> SimpleNamespace:
        return SimpleNamespace(
            id=9000,
            filename="evidence.png",
            size=9700,
            content_type="image/png",
            description=None,
            is_spoiler=lambda: False,
            save=save,
        )

    async def test_original_cdn_is_used_before_media_proxy(self) -> None:
        calls: list[bool] = []

        async def save(path: Path, *, use_cached: bool) -> None:
            calls.append(use_cached)
            Path(path).write_bytes(b"a-valid-file-with-a-different-reported-size")

        result = await sgl_archive._archive_attachment(
            self._attachment(AsyncMock(side_effect=save)),
            message_id=8000,
            directory=self.directory,
        )
        self.assertEqual(calls, [False])
        self.assertEqual(result["download_variant"], "original_cdn")
        self.assertEqual(result["discord_reported_size"], 9700)
        self.assertNotEqual(result["size"], result["discord_reported_size"])

    async def test_proxy_is_only_used_when_original_download_fails(self) -> None:
        calls: list[bool] = []

        async def save(path: Path, *, use_cached: bool) -> None:
            calls.append(use_cached)
            if not use_cached:
                raise RuntimeError("original unavailable")
            Path(path).write_bytes(b"proxy-copy")

        result = await sgl_archive._archive_attachment(
            self._attachment(AsyncMock(side_effect=save)),
            message_id=8001,
            directory=self.directory,
        )
        self.assertEqual(calls, [False, True])
        self.assertEqual(result["download_variant"], "media_proxy")
        self.assertEqual(result["size"], len(b"proxy-copy"))

    async def test_failed_variants_leave_no_partial_file(self) -> None:
        async def save(path: Path, *, use_cached: bool) -> None:
            Path(path).write_bytes(b"partial")
            raise RuntimeError(f"failed:{use_cached}")

        with self.assertRaisesRegex(OSError, "attachment_download_failed"):
            await sgl_archive._archive_attachment(
                self._attachment(AsyncMock(side_effect=save)),
                message_id=8002,
                directory=self.directory,
            )
        self.assertEqual(list(self.directory.glob("*.part")), [])
        self.assertEqual(list(self.directory.glob("8002-*")), [])


class SGLArchiveDeletionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_maintenance_sweeps_existing_archive_and_sets_marker(self) -> None:
        guild = SimpleNamespace(id=10)
        channel = SimpleNamespace(id=1000, name="090-closed", topic=None)
        category = SimpleNamespace(id=2000, text_channels=[channel])
        bot = SimpleNamespace(guilds=[guild])
        with (
            patch.object(sgl_archive, "SGBUREAU_ARCHIVE_INITIAL_PURGE", True),
            patch.object(sgl_archive, "_resolve_category", AsyncMock(return_value=category)),
            patch.object(sgl_archive, "_reconcile_deleted_sources", AsyncMock()),
            patch.object(sgl_archive, "_purge_expired_restorations", AsyncMock()),
            patch.object(
                sgl_archive.storage,
                "is_sgl_archive_backfill_complete",
                return_value=False,
            ),
            patch.object(
                sgl_archive.storage,
                "get_active_sgl_restoration_by_channel",
                return_value=None,
            ),
            patch.object(
                sgl_archive.storage, "get_sgl_case_by_channel", return_value=None
            ),
            patch.object(
                sgl_archive, "snapshot_and_delete_case_channel", AsyncMock()
            ) as snapshot,
            patch.object(
                sgl_archive.storage, "mark_sgl_archive_backfill_complete"
            ) as mark_complete,
            patch.object(sgl_archive, "log_technical_event", AsyncMock()),
            patch.object(sgl_archive.asyncio, "sleep", AsyncMock()),
        ):
            await sgl_archive.run_sgl_archive_maintenance_once(bot)
        snapshot.assert_awaited_once_with(
            bot, channel, case=None, case_number=90
        )
        mark_complete.assert_called_once_with(10, 2000)

    async def test_failed_initial_channel_keeps_backfill_pending(self) -> None:
        guild = SimpleNamespace(id=10)
        channel = SimpleNamespace(id=1000, name="090-closed", topic=None)
        category = SimpleNamespace(id=2000, text_channels=[channel])
        bot = SimpleNamespace(guilds=[guild])
        with (
            patch.object(sgl_archive, "SGBUREAU_ARCHIVE_INITIAL_PURGE", True),
            patch.object(sgl_archive, "_resolve_category", AsyncMock(return_value=category)),
            patch.object(sgl_archive, "_reconcile_deleted_sources", AsyncMock()),
            patch.object(sgl_archive, "_purge_expired_restorations", AsyncMock()),
            patch.object(
                sgl_archive.storage,
                "is_sgl_archive_backfill_complete",
                return_value=False,
            ),
            patch.object(
                sgl_archive.storage,
                "get_active_sgl_restoration_by_channel",
                return_value=None,
            ),
            patch.object(
                sgl_archive.storage, "get_sgl_case_by_channel", return_value=None
            ),
            patch.object(
                sgl_archive,
                "snapshot_and_delete_case_channel",
                AsyncMock(side_effect=RuntimeError("capture failed")),
            ),
            patch.object(
                sgl_archive.storage, "mark_sgl_archive_backfill_complete"
            ) as mark_complete,
        ):
            await sgl_archive.run_sgl_archive_maintenance_once(bot)
        mark_complete.assert_not_called()

    async def test_channel_is_not_deleted_when_capture_fails(self) -> None:
        guild = SimpleNamespace(id=10)
        channel = SimpleNamespace(
            id=1000,
            name="090-closed",
            guild=guild,
            delete=AsyncMock(),
        )
        bot = SimpleNamespace()
        with (
            patch.object(sgl_archive.storage, "get_sgl_case_archive_by_source", return_value=None),
            patch.object(
                sgl_archive,
                "capture_case_channel",
                AsyncMock(side_effect=RuntimeError("capture failed")),
            ),
            patch.object(sgl_archive, "log_technical_event", AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                await sgl_archive.snapshot_and_delete_case_channel(
                    bot, channel, case=None, case_number=90
                )
        channel.delete.assert_not_awaited()

    async def test_verified_snapshot_is_deleted_then_sealed(self) -> None:
        guild = SimpleNamespace(id=10)
        channel = SimpleNamespace(
            id=1000,
            name="090-closed",
            guild=guild,
            delete=AsyncMock(),
        )
        archive = SimpleNamespace(
            id=7,
            status="ready",
            source_deleted_at=None,
        )
        sealed = SimpleNamespace(id=7, status="sealed")
        with (
            patch.object(
                sgl_archive.storage,
                "get_sgl_case_archive_by_source",
                return_value=archive,
            ),
            patch.object(sgl_archive, "verify_archive_files", AsyncMock()),
            patch.object(
                sgl_archive.storage,
                "mark_sgl_archive_source_deleted",
                return_value=sealed,
            ) as mark_deleted,
        ):
            result = await sgl_archive.snapshot_and_delete_case_channel(
                SimpleNamespace(), channel, case=None, case_number=90
            )
        channel.delete.assert_awaited_once()
        mark_deleted.assert_called_once_with(7)
        self.assertIs(result, sealed)


if __name__ == "__main__":
    unittest.main()
