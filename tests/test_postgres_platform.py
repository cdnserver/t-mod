from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from persistence.postgres_compat import (
    postgres_enabled,
    split_sql_script,
    translate_sql,
)
from scripts.migrate_sqlite_to_postgres import _coerce_value, _json_default


ROOT = Path(__file__).resolve().parents[1]


class PostgresCompatibilityTests(unittest.TestCase):
    def test_legacy_values_are_validated_before_postgres_copy(self) -> None:
        self.assertEqual(_coerce_value("1488", "bigint", False), 1488)
        self.assertEqual(_coerce_value("true", "boolean", False), True)
        self.assertEqual(_coerce_value("hello\x00world", "text", False), "helloworld")
        with self.assertRaisesRegex(ValueError, "invalid_integer"):
            _coerce_value("RU15", "bigint", False)
        self.assertEqual(_json_default(b"tmod"), {"type": "bytes", "base64": "dG1vZA=="})

    def test_backend_selection_is_explicit_or_database_url_driven(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(postgres_enabled())
        with patch.dict(os.environ, {"TMOD_DATABASE_BACKEND": "postgresql"}, clear=True):
            self.assertTrue(postgres_enabled())
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://example/tmod"}, clear=True):
            self.assertTrue(postgres_enabled())

    def test_postgres_password_uses_persistent_fallback_file(self) -> None:
        from persistence.postgres_compat import _secret

        with tempfile.TemporaryDirectory() as directory:
            fallback = Path(directory) / "postgres-password.txt"
            fallback.write_text("safe-password", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "POSTGRES_PASSWORD_FILE": "/missing/runtime-secret",
                    "POSTGRES_PASSWORD_FALLBACK_FILE": str(fallback),
                },
                clear=True,
            ):
                self.assertEqual(
                    _secret("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE"),
                    "safe-password",
                )
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

    def test_sqlite_julianday_ordering_translates_to_postgres(self) -> None:
        translated = translate_sql(
            "SELECT due_at FROM delivery_outbox ORDER BY julianday(due_at), due_at"
        )
        self.assertNotIn("julianday", translated.lower())
        self.assertIn("EXTRACT(EPOCH FROM ((due_at)::timestamptz))", translated)

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

        bot_service = compose.split("  tmod-web:", 1)[0].split(
            "  tmod-discord-bot:", 1
        )[1]
        self.assertIn(
            'POSTGRES_PASSWORD_FILE: "/app/persistent/secrets/postgres-password.txt"',
            compose,
        )
        self.assertIn(
            'POSTGRES_PASSWORD_FALLBACK_FILE: "/run/secrets/postgres_password"',
            compose,
        )
        self.assertIn(
            '"C:/Users/Admin/Documents/SGLDiscordBot:/app/persistent"',
            bot_service,
        )

    def test_runtime_uses_matching_postgres_17_backup_tools(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM postgres:17-bookworm AS postgres-tools", dockerfile)
        self.assertIn(
            "COPY --from=postgres-tools /usr/lib/postgresql/17/bin/pg_dump",
            dockerfile,
        )
        self.assertIn(
            "COPY --from=postgres-tools /usr/lib/postgresql/17/bin/pg_restore",
            dockerfile,
        )
        self.assertIn(
            "COPY --from=postgres-tools /usr/lib/x86_64-linux-gnu/libpq.so.5*",
            dockerfile,
        )
        self.assertIn("RUN ldconfig", dockerfile)
        self.assertIn('pg_dump --version | grep -F "PostgreSQL) 17."', dockerfile)

    def test_safe_update_backs_up_the_active_postgres_database(self) -> None:
        updater = (ROOT / "safe_update_windows.ps1").read_text(encoding="utf-8")
        postgres_gate = updater.index("to_regclass('public.tmod_platform_migrations')")
        legacy_gate = updater.index("docker inspect tmod-discord-bot", postgres_gate)
        active_path = updater[postgres_gate:legacy_gate]

        self.assertIn("Could not verify the active PostgreSQL database", active_path)
        self.assertIn("docker exec tmod-postgres pg_dump", active_path)
        self.assertIn("docker exec tmod-postgres pg_restore --list", active_path)
        self.assertIn('docker cp "tmod-postgres:$containerPath"', active_path)
        self.assertIn('backend = "postgresql"', active_path)
        self.assertIn('return "/app/persistent/backups/database/$name"', active_path)


if __name__ == "__main__":
    unittest.main()
