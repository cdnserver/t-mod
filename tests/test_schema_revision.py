from __future__ import annotations
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from persistence import schema
from persistence.schema_revision import MARKER, postgres_schema_current, schema_revision, workspace_open_index_current
from scripts.migrate_sqlite_to_postgres import _acquire_migration_lock


class SchemaRevisionTests(unittest.TestCase):
    def test_revision_is_stable_and_content_based(self) -> None:
        self.assertEqual(schema_revision(), schema_revision())
        self.assertEqual(len(schema_revision()), 64)
        with patch("persistence.schema_revision.Path.read_bytes", return_value=b"changed schema"):
            self.assertNotEqual(schema_revision(), self.revision)

    def setUp(self) -> None:
        self.revision = schema_revision()

    def test_gate_requires_existing_meta_and_exact_revision(self) -> None:
        con = MagicMock()
        con.execute.return_value.fetchone.side_effect = [(None,)]
        self.assertFalse(postgres_schema_current(con, "current"))
        con.execute.return_value.fetchone.side_effect = [("meta",), ("old",)]
        self.assertFalse(postgres_schema_current(con, "current"))
        con.execute.return_value.fetchone.side_effect = [("meta",), ("current",)]
        self.assertTrue(postgres_schema_current(con, "current"))
        self.assertEqual(con.execute.call_args.args[1], (MARKER,))

    def test_unchanged_postgres_startup_runs_no_ddl_or_updates(self) -> None:
        con = MagicMock()
        con.__enter__.return_value = con
        with patch.object(schema._core, "postgres_enabled", return_value=True), patch.object(schema, "connect", return_value=con), patch.object(schema, "postgres_schema_current", return_value=True), patch.object(schema, "schema_revision", return_value="same"):
            schema.init_db()
        con.execute.assert_not_called()
        con.executescript.assert_not_called()
        con.commit.assert_called_once()

    def test_gate_is_rechecked_after_other_process_applies_schema(self) -> None:
        con = MagicMock(); con.__enter__.return_value = con
        with patch.object(schema._core, "postgres_enabled", return_value=True), patch.object(schema, "connect", return_value=con), patch.object(schema, "postgres_schema_current", side_effect=[False, True]), patch.object(schema, "schema_revision", return_value="same"):
            schema.init_db()
        self.assertEqual(len(con.execute.call_args_list), 1)
        self.assertIn("pg_advisory_xact_lock", con.execute.call_args.args[0])
        con.executescript.assert_not_called()

    def test_forced_post_import_upgrade_cannot_skip_data_migrations(self) -> None:
        con = MagicMock(); con.__enter__.return_value = con
        con.executescript.side_effect = RuntimeError("schema transaction attempted")
        with patch.object(schema._core, "postgres_enabled", return_value=True), patch.object(schema, "connect", return_value=con), patch.object(schema, "postgres_schema_current", return_value=True) as gate, patch.object(schema, "schema_revision", return_value="same"):
            with self.assertRaisesRegex(RuntimeError, "schema transaction attempted"):
                schema.init_db(force=True)
        gate.assert_not_called()
        con.commit.assert_not_called()
        self.assertIn("lock_timeout", con.execute.call_args.args[0])

    def test_current_sqlite_index_is_preserved_and_old_predicate_rejected(self) -> None:
        with sqlite3.connect(":memory:") as con:
            con.execute("CREATE TABLE tvrs_bill_workspaces(guild_id,author_id,status)")
            con.execute("CREATE UNIQUE INDEX idx_tvrs_bill_workspaces_one_open ON tvrs_bill_workspaces(guild_id,author_id) WHERE status IN ('draft','review')")
            self.assertTrue(workspace_open_index_current(con, postgres=False))
            con.execute("DROP INDEX idx_tvrs_bill_workspaces_one_open")
            con.execute("CREATE UNIQUE INDEX idx_tvrs_bill_workspaces_one_open ON tvrs_bill_workspaces(guild_id,author_id) WHERE status IN ('draft','submitted')")
            self.assertFalse(workspace_open_index_current(con, postgres=False))

    def test_postgres_index_validation_checks_validity_columns_and_predicate(self) -> None:
        con = MagicMock()
        valid = [True, True, "guild_id", "author_id", 2, "(status = ANY (ARRAY['draft'::text, 'review'::text]))"]
        con.execute.return_value.fetchone.return_value = valid
        self.assertTrue(workspace_open_index_current(con, postgres=True))
        for index, value in [(0,False),(1,False),(2,"other"),(4,3),(5,"(status = 'draft'::text)")]:
            changed = valid.copy(); changed[index] = value
            con.execute.return_value.fetchone.return_value = changed
            self.assertFalse(workspace_open_index_current(con, postgres=True))

    def test_migration_lock_is_bounded_and_never_waits_in_postgres(self) -> None:
        pg = MagicMock(); cursor = pg.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(False,), (True,)]
        with patch("scripts.migrate_sqlite_to_postgres.time.sleep") as sleep:
            _acquire_migration_lock(pg)
        self.assertIn("pg_try_advisory_lock", cursor.execute.call_args.args[0])
        sleep.assert_called_once_with(1)
        cursor.fetchone.side_effect = [(False,)]
        with patch("scripts.migrate_sqlite_to_postgres.time.monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(RuntimeError, "postgres_migration_busy"):
                _acquire_migration_lock(pg)
