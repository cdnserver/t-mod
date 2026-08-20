import gc
import tempfile
import unittest
from pathlib import Path

from persistence import core, schema, sgl_repository


class SGLCaseOperationsRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_database_file = core.DATA_DIR, core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "sgl-operations.db"
        schema.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=88,
            client_id=11,
            client_display="Client",
            lead_lawyer_id=22,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=22,
            created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None

    def tearDown(self) -> None:
        core.DATA_DIR, core.DATABASE_FILE = self.old_data_dir, self.old_database_file
        gc.collect()
        self.temp_dir.cleanup()

    def test_task_is_durable_and_drives_server_side_queue(self) -> None:
        task = sgl_repository.create_sgl_case_task(
            case=self.case,
            title="Проверить доказательства",
            description="Нужны две ссылки на оплату.",
            priority="high",
            owner_id=22,
            owner_display="Lawyer",
            due_at="2026-08-21T10:00:00+00:00",
            source="atlas",
            created_by_id=22,
            created_by_display="Lawyer",
        )
        snapshot = sgl_repository.build_sgl_operations_snapshot(88)
        task_item = next(item for item in snapshot["queue"] if item["id"] == f"task:{task['id']}")
        self.assertEqual(task_item["title"], "Проверить доказательства")
        self.assertEqual(task_item["priority"], "high")

        updated = sgl_repository.update_sgl_case_task(
            task_id=task["id"], guild_id=88, values={"status": "done"},
            actor_id=22, actor_display="Lawyer",
        )
        assert updated is not None
        self.assertEqual(updated["status"], "done")
        self.assertIsNotNone(updated["completed_at"])
        self.assertNotIn(
            f"task:{task['id']}",
            [item["id"] for item in sgl_repository.build_sgl_operations_snapshot(88)["queue"]],
        )

    def test_bureau_task_list_includes_case_context_and_filters(self) -> None:
        mine = sgl_repository.create_sgl_case_task(
            case=self.case,
            title="Моя открытая задача",
            priority="high",
            owner_id=22,
            owner_display="Lawyer",
            created_by_id=22,
            created_by_display="Lawyer",
        )
        other = sgl_repository.create_sgl_case_task(
            case=self.case,
            title="Закрытая задача другого сотрудника",
            priority="low",
            owner_id=99,
            owner_display="Other lawyer",
            created_by_id=22,
            created_by_display="Lawyer",
        )
        closed = sgl_repository.update_sgl_case_task(
            task_id=other["id"], guild_id=88, values={"status": "done"},
            actor_id=22, actor_display="Lawyer",
        )
        assert closed is not None

        tasks = sgl_repository.list_sgl_case_tasks_for_guild(
            88, status="open", owner_id=22,
        )

        self.assertEqual([task["id"] for task in tasks], [mine["id"]])
        self.assertEqual(tasks[0]["case_number"], self.case.case_number)
        self.assertEqual(tasks[0]["case_title"], "Client")
        self.assertEqual(tasks[0]["case_status"], self.case.status)
        with self.assertRaisesRegex(ValueError, "sgl_task_status_invalid"):
            sgl_repository.list_sgl_case_tasks_for_guild(88, status="invalid")

    def test_notification_acknowledgement_is_auditable(self) -> None:
        notification = sgl_repository.create_sgl_case_notification(
            guild_id=88,
            case=self.case,
            kind="forum_changed",
            severity="warning",
            title="Иск изменился",
            body="В теме появились новые обстоятельства.",
            tab="forum",
            source="forum_watch",
            external_status="sent",
            dedupe_key="forum:1:changed:1",
        )
        inbox = sgl_repository.list_sgl_case_notifications(
            guild_id=88, include_acknowledged=False,
        )
        self.assertEqual(inbox[0]["id"], notification["id"])

        acknowledged = sgl_repository.acknowledge_sgl_case_notification(
            notification["id"], guild_id=88, actor_id=22, actor_display="Lawyer",
        )
        assert acknowledged is not None
        self.assertIsNotNone(acknowledged["acknowledged_at"])
        self.assertEqual(
            sgl_repository.list_sgl_case_notifications(guild_id=88, include_acknowledged=False),
            [],
        )
        self.assertEqual(
            sgl_repository.get_sgl_case_events(self.case.id, 1)[0]["action"],
            "notification_acknowledged",
        )

    def test_notification_history_filters_acknowledgement_delivery_and_source(self) -> None:
        matched = sgl_repository.create_sgl_case_notification(
            guild_id=88,
            case=self.case,
            kind="forum_changed",
            severity="warning",
            title="Изменённый иск",
            source="forum_watch",
            external_status="sent",
        )
        sgl_repository.create_sgl_case_notification(
            guild_id=88,
            case=self.case,
            kind="system",
            severity="info",
            title="Служебный сигнал",
            source="system",
            external_status="not_applicable",
        )
        acknowledged = sgl_repository.acknowledge_sgl_case_notification(
            matched["id"], guild_id=88, actor_id=22, actor_display="Lawyer",
        )
        assert acknowledged is not None

        history = sgl_repository.list_sgl_case_notifications(
            guild_id=88,
            case_number=self.case.case_number,
            severity="warning",
            acknowledged=True,
            external_status="sent",
            source="forum_watch",
        )

        self.assertEqual([item["id"] for item in history], [matched["id"]])
        self.assertEqual(history[0]["acknowledged_by_display"], "Lawyer")
        self.assertEqual(
            sgl_repository.list_sgl_case_notifications(
                guild_id=88, case_number=self.case.case_number, acknowledged=False,
            )[0]["title"],
            "Служебный сигнал",
        )
        with self.assertRaisesRegex(ValueError, "sgl_notification_severity_invalid"):
            sgl_repository.list_sgl_case_notifications(guild_id=88, severity="urgent")

    def test_forum_snapshots_keep_a_before_and_after_projection(self) -> None:
        draft = sgl_repository.create_sgl_case_forum_publication(
            case=self.case,
            target_url="https://forum.majestic-rp.ru/forums/court.42/",
            title="Иск SGL",
            body="Текст иска",
            created_by_id=22,
            created_by_display="Lawyer",
        )
        claimed = sgl_repository.claim_sgl_case_forum_publication(
            guild_id=88, case_number=self.case.case_number, publication_id=draft["id"],
            actor_id=22, actor_display="Lawyer",
        )
        assert claimed is not None
        publication = sgl_repository.mark_sgl_case_forum_publication_published(
            publication_id=draft["id"], forum_url="https://forum.majestic-rp.ru/threads/claim.8/",
        )
        assert publication is not None
        sgl_repository.observe_sgl_case_forum_publication(
            publication=publication, thread_title="Иск", thread_excerpt="До изменения", content_fingerprint="a" * 64,
        )
        sgl_repository.observe_sgl_case_forum_publication(
            publication=publication, thread_title="Иск", thread_excerpt="После изменения", content_fingerprint="b" * 64,
        )
        snapshots = sgl_repository.list_sgl_case_forum_snapshots(self.case.id)
        self.assertEqual([item["snapshot_kind"] for item in snapshots], ["changed", "initial"])
        self.assertEqual(snapshots[0]["thread_excerpt"], "После изменения")
        self.assertEqual(snapshots[1]["thread_excerpt"], "До изменения")


if __name__ == "__main__":
    unittest.main()
