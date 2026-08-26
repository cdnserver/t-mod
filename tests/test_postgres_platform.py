from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from persistence.postgres_compat import (
    postgres_enabled,
    split_sql_script,
    translate_sql,
)


ROOT = Path(__file__).resolve().parents[1]


class PostgresCompatibilityTests(unittest.TestCase):
    def test_backend_selection_is_explicit_or_database_url_driven(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(postgres_enabled())
        with patch.dict(os.environ, {"TMOD_DATABASE_BACKEND": "postgresql"}, clear=True):
            self.assertTrue(postgres_enabled())
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://example/tmod"}, clear=True):
            self.assertTrue(postgres_enabled())

    def test_sqlite_repository_dialect_translates_without_changing_aggregates(self) -> None:
        translated = translate_sql(
            """
            INSERT OR IGNORE INTO sample(guild_id, value)
            VALUES (?, MAX(0, ?))
            """
        )
        self.assertIn("INSERT INTO sample", translated)
        self.assertIn("VALUES (%s, GREATEST(0, %s))", translated)
        self.assertIn("ON CONFLICT DO NOTHING", translated)
        aggregate = translate_sql("SELECT MAX(value), MIN(a, b) FROM sample")
        self.assertIn("MAX(value)", aggregate)
        self.assertIn("LEAST(a, b)", aggregate)

    def test_schema_types_and_unicode_search_translate(self) -> None:
        translated = translate_sql(
            "CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT, n INTEGER, r REAL, b BLOB)"
        )
        self.assertIn("id BIGSERIAL PRIMARY KEY", translated)
        self.assertIn("n BIGINT", translated)
        self.assertIn("r DOUBLE PRECISION", translated)
        self.assertIn("b BYTEA", translated)
        search = translate_sql(
            "SELECT * FROM x WHERE T_CASEFOLD(name)=T_CASEFOLD(?) ORDER BY name COLLATE NOCASE"
        )
        self.assertIn("LOWER(name)=LOWER(%s)", search)
        self.assertNotIn("NOCASE", search)

    def test_script_splitter_does_not_break_quoted_semicolons(self) -> None:
        self.assertEqual(
            split_sql_script("INSERT INTO x(v) VALUES ('a;b'); SELECT 1;"),
            ["INSERT INTO x(v) VALUES ('a;b')", "SELECT 1"],
        )

    def test_compose_has_isolated_postgres_migration_gateway_and_worker(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('image: postgres:17-alpine', compose)
        self.assertIn('TMOD_DATABASE_BACKEND: "postgresql"', compose)
        self.assertIn('condition: service_completed_successfully', compose)
        self.assertIn('TMOD_INTERNAL_WEB_UPSTREAM: "http://tmod-discord-bot:8788"', compose)
        self.assertIn('tmod-data:\n    internal: true', compose)
        self.assertNotIn('"5432:5432"', compose)


if __name__ == "__main__":
    unittest.main()
