"""Small DB-API compatibility layer used while T-Mod moves from SQLite to PostgreSQL.

Repositories deliberately keep their compact SQLite-style SQL for now.  This
adapter translates the limited dialect used by T-Mod and exposes rows with the
same indexed/keyed access as ``sqlite3.Row``.  It is intentionally private to
the persistence package; new code should only call ``persistence.core.connect``.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any


_pool: Any = None
_pool_lock = threading.Lock()


def postgres_enabled() -> bool:
    backend = os.getenv("TMOD_DATABASE_BACKEND", "").strip().lower()
    return backend in {"postgres", "postgresql"} or bool(
        os.getenv("DATABASE_URL", "").strip()
    )


def _secret(name: str, file_name: str, default: str = "") -> str:
    direct = os.getenv(name, "").strip()
    if direct:
        return direct
    path = os.getenv(file_name, "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"postgres_secret_unavailable:{path}") from exc
    return default


def postgres_settings() -> dict[str, Any]:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return {"conninfo": url}
    password = _secret("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE")
    if not password:
        raise RuntimeError("postgres_password_not_configured")
    return {
        "host": os.getenv("POSTGRES_HOST", "tmod-postgres").strip(),
        "port": int(os.getenv("POSTGRES_PORT", "5432") or 5432),
        "dbname": os.getenv("POSTGRES_DB", "tmod").strip(),
        "user": os.getenv("POSTGRES_USER", "tmod").strip(),
        "password": password,
        "connect_timeout": int(os.getenv("POSTGRES_CONNECT_TIMEOUT", "10") or 10),
        "application_name": os.getenv(
            "POSTGRES_APPLICATION_NAME", "tmod"
        ).strip(),
    }


def postgres_safe_target() -> str:
    settings = postgres_settings()
    if "conninfo" in settings:
        # Never expose credentials in status output.
        value = str(settings["conninfo"])
        return re.sub(r"//[^/@]+@", "//***@", value)
    return "postgresql://{host}:{port}/{dbname}".format(**settings)


def _load_driver() -> tuple[Any, Any]:
    try:
        import psycopg  # type: ignore
        from psycopg_pool import ConnectionPool  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL selected but psycopg is not installed"
        ) from exc
    return psycopg, ConnectionPool


def _connection_pool() -> Any:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        _, pool_type = _load_driver()
        settings = postgres_settings()
        conninfo = settings.pop("conninfo", "")
        max_size = max(4, min(50, int(os.getenv("POSTGRES_POOL_MAX", "20") or 20)))
        _pool = pool_type(
            conninfo=conninfo,
            kwargs=settings,
            min_size=0,
            max_size=max_size,
            timeout=15,
            open=True,
        )
        return _pool


def raw_postgres_connection(*, autocommit: bool = False) -> Any:
    """Return an unmanaged psycopg connection for migration/backup tooling."""
    psycopg, _ = _load_driver()
    settings = postgres_settings()
    conninfo = settings.pop("conninfo", "")
    return psycopg.connect(conninfo, autocommit=autocommit, **settings)


class CompatRow(Mapping[str, Any]):
    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        self._names = tuple(str(item) for item in names)
        self._values = tuple(values)
        self._index = {name: position for position, name in enumerate(self._names)}

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._index[str(key)]]

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> tuple[str, ...]:  # type: ignore[override]
        return self._names


class CompatCursor:
    def __init__(self, cursor: Any, *, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid
        self._rowcount_override: int | None = None

    @property
    def rowcount(self) -> int:
        return (
            self._rowcount_override
            if self._rowcount_override is not None
            else int(self._cursor.rowcount)
        )

    @rowcount.setter
    def rowcount(self, value: int) -> None:
        self._rowcount_override = int(value)

    @property
    def description(self) -> Any:
        return self._cursor.description

    def _row(self, value: Any) -> CompatRow | None:
        if value is None:
            return None
        names = [item.name for item in (self._cursor.description or ())]
        return CompatRow(names, value)

    def fetchone(self) -> CompatRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[CompatRow]:
        return [self._row(row) for row in self._cursor.fetchall()]  # type: ignore[misc]

    def fetchmany(self, size: int | None = None) -> list[CompatRow]:
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._row(row) for row in rows]  # type: ignore[misc]

    def __iter__(self) -> Iterator[CompatRow]:
        for row in self._cursor:
            converted = self._row(row)
            if converted is not None:
                yield converted


class VirtualCursor:
    def __init__(self, names: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        self._names = tuple(names)
        self._rows = [CompatRow(self._names, row) for row in rows]
        self._position = 0
        self.lastrowid: int | None = None
        self.rowcount = len(self._rows)
        self.description = tuple(type("Column", (), {"name": name})() for name in self._names)

    def fetchone(self) -> CompatRow | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> list[CompatRow]:
        result = self._rows[self._position :]
        self._position = len(self._rows)
        return result

    def fetchmany(self, size: int | None = None) -> list[CompatRow]:
        selected = 1 if size is None else max(0, int(size))
        result = self._rows[self._position : self._position + selected]
        self._position += len(result)
        return result

    def __iter__(self) -> Iterator[CompatRow]:
        return iter(self.fetchall())


def _replace_placeholders(sql: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append("%%" if char == "%" else char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        elif char == "%":
            output.append("%%")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _rewrite_scalar_minmax(sql: str) -> str:
    """Translate SQLite scalar MAX/MIN while preserving aggregate calls."""
    pattern = re.compile(r"\b(MAX|MIN)\s*\(", re.IGNORECASE)
    cursor = 0
    output: list[str] = []
    while True:
        match = pattern.search(sql, cursor)
        if not match:
            output.append(sql[cursor:])
            break
        output.append(sql[cursor : match.start()])
        depth = 1
        quote: str | None = None
        comma = False
        position = match.end()
        while position < len(sql) and depth:
            char = sql[position]
            if quote:
                if char == quote:
                    if position + 1 < len(sql) and sql[position + 1] == quote:
                        position += 1
                    else:
                        quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 1:
                comma = True
            position += 1
        name = match.group(1).upper()
        output.append(("GREATEST" if name == "MAX" else "LEAST") if comma else name)
        output.append(sql[match.end() - 1 : position])
        cursor = position
    return "".join(output)


def translate_sql(sql: str) -> str:
    value = str(sql).strip()
    if not value:
        return value
    value = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\bAUTOINCREMENT\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bINTEGER\b", "BIGINT", value, flags=re.IGNORECASE)
    value = re.sub(r"\bREAL\b", "DOUBLE PRECISION", value, flags=re.IGNORECASE)
    value = re.sub(r"\bBLOB\b", "BYTEA", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+COLLATE\s+NOCASE\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bT_CASEFOLD\s*\(", "LOWER(", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\(\s*julianday\(completed_at\)\s*-\s*julianday\(due_at\)\s*\)\s*\*\s*86400",
        "EXTRACT(EPOCH FROM (completed_at::timestamptz - due_at::timestamptz))",
        value,
        flags=re.IGNORECASE,
    )
    insert_ignore = bool(re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO\b", value, re.IGNORECASE))
    if insert_ignore:
        value = re.sub(
            r"^INSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
        semicolon = ";" if value.endswith(";") else ""
        value = value.removesuffix(";").rstrip() + " ON CONFLICT DO NOTHING" + semicolon
    value = _rewrite_scalar_minmax(value)
    return _replace_placeholders(value)


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0
    while index < len(script):
        char = script[index]
        following = script[index + 1] if index + 1 < len(script) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
                buffer.append(" ")
        elif block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 1
                buffer.append(" ")
        elif quote:
            buffer.append(char)
            if char == quote:
                if following == quote:
                    buffer.append(following)
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            buffer.append(char)
        elif char == "-" and following == "-":
            line_comment = True
            index += 1
        elif char == "/" and following == "*":
            block_comment = True
            index += 1
        elif char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
        else:
            buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


class PostgresCompatConnection:
    def __init__(self, connection: Any, pool: Any, *, readonly: bool = False) -> None:
        self._connection = connection
        self._pool = pool
        self._readonly = readonly
        self._closed = False
        self._last_insert_id: int | None = None
        self._serial_cache: dict[str, str | None] = {}
        if readonly:
            self._connection.execute("SET TRANSACTION READ ONLY")

    def _virtual_pragma(self, sql: str) -> VirtualCursor | CompatCursor | None:
        lowered = sql.strip().rstrip(";").lower()
        table_match = re.match(r"pragma\s+table_info\s*\(\s*([\w]+)\s*\)", lowered)
        if table_match:
            cursor = self._connection.execute(
                """
                SELECT ordinal_position - 1 AS cid, column_name AS name,
                       data_type AS type, CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
                       column_default AS dflt_value, 0 AS pk
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_match.group(1),),
            )
            return CompatCursor(cursor)
        if lowered.startswith("pragma journal_mode"):
            return VirtualCursor(("journal_mode",), (("postgresql",),))
        if lowered.startswith("pragma page_count"):
            cursor = self._connection.execute(
                "SELECT CEIL(pg_database_size(current_database()) / 4096.0)::bigint AS page_count"
            )
            return CompatCursor(cursor)
        if lowered.startswith("pragma quick_check") or lowered.startswith("pragma integrity_check"):
            return VirtualCursor(("integrity_check",), (("ok",),))
        if lowered.startswith("pragma "):
            return VirtualCursor(("pragma",), ())
        return None

    def _serial_sequence(self, table: str) -> str | None:
        normalized = table.strip('"').lower()
        if normalized in self._serial_cache:
            return self._serial_cache[normalized]
        cursor = self._connection.execute(
            """
            SELECT pg_get_serial_sequence(%s, 'id')
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = 'id'
            """,
            (normalized, normalized),
        )
        row = cursor.fetchone()
        sequence = str(row[0]) if row and row[0] else None
        self._serial_cache[normalized] = sequence
        return sequence

    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        pragma = self._virtual_pragma(str(sql))
        if pragma is not None:
            return pragma
        raw = str(sql).strip()
        if re.match(r"^DELETE\s+FROM\s+sqlite_sequence\b", raw, re.IGNORECASE):
            # PostgreSQL sequences are monotonic and need not be rewound after
            # a domain reset. Avoiding reuse is safer for audit identifiers.
            return VirtualCursor((), ())
        if re.match(r"^SELECT\s+seq\s+FROM\s+sqlite_sequence\b", raw, re.IGNORECASE):
            return VirtualCursor(("seq",), ())
        if re.match(r"^BEGIN\s+IMMEDIATE\b", raw, re.IGNORECASE):
            if self._connection.info.transaction_status.name == "IDLE":
                self._connection.execute("BEGIN")
            return VirtualCursor((), ())
        if re.match(r"^SELECT\s+last_insert_rowid\s*\(\s*\)", raw, re.IGNORECASE):
            return VirtualCursor(("last_insert_rowid",), (((self._last_insert_id or 0),),))

        translated = translate_sql(raw)
        insert_match = re.match(r"^INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+([\w\"]+)", raw, re.IGNORECASE)
        returning_id = False
        if insert_match and not re.search(r"\bRETURNING\b", translated, re.IGNORECASE):
            try:
                returning_id = self._serial_sequence(insert_match.group(1)) is not None
            except Exception:
                returning_id = False
            if returning_id:
                translated = translated.rstrip().removesuffix(";").rstrip() + " RETURNING id"

        cursor = self._connection.cursor()
        savepoint = f"tmod_{id(cursor):x}"
        # Expected uniqueness failures are caught by several repositories.
        # Isolate mutating statements so those catches can continue the same
        # transaction, while keeping hot read paths to one PostgreSQL roundtrip.
        use_savepoint = not re.match(
            r"^(SELECT|WITH|SHOW|SET|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
            translated,
            re.IGNORECASE,
        )
        try:
            if use_savepoint:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            cursor.execute(translated, parameters)
            lastrowid = None
            if returning_id:
                inserted = cursor.fetchone()
                if inserted:
                    lastrowid = int(inserted[0])
                    self._last_insert_id = lastrowid
            if use_savepoint:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return CompatCursor(cursor, lastrowid=lastrowid)
        except Exception as exc:
            if use_savepoint:
                try:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    pass
            psycopg, _ = _load_driver()
            if isinstance(exc, psycopg.IntegrityError):
                raise sqlite3.IntegrityError(str(exc)) from exc
            if isinstance(exc, psycopg.OperationalError):
                raise sqlite3.OperationalError(str(exc)) from exc
            if isinstance(exc, psycopg.DatabaseError):
                raise sqlite3.DatabaseError(str(exc)) from exc
            raise

    def executemany(self, sql: str, parameters: Sequence[Sequence[Any]]) -> Any:
        translated = translate_sql(str(sql).strip())
        cursor = self._connection.cursor()
        savepoint = f"tmod_many_{id(cursor):x}"
        try:
            self._connection.execute(f"SAVEPOINT {savepoint}")
            cursor.executemany(translated, parameters)
            self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return CompatCursor(cursor)
        except Exception as exc:
            try:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                pass
            psycopg, _ = _load_driver()
            if isinstance(exc, psycopg.IntegrityError):
                raise sqlite3.IntegrityError(str(exc)) from exc
            if isinstance(exc, psycopg.OperationalError):
                raise sqlite3.OperationalError(str(exc)) from exc
            if isinstance(exc, psycopg.DatabaseError):
                raise sqlite3.DatabaseError(str(exc)) from exc
            raise

    def executescript(self, script: str) -> None:
        for statement in split_sql_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._connection.info.transaction_status.name != "IDLE":
                self._connection.rollback()
        finally:
            self._pool.putconn(self._connection)

    def __enter__(self) -> "PostgresCompatConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            self.rollback() if exc_type else self.commit()
        finally:
            self.close()
        return False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def connect_postgres(*, readonly: bool = False) -> PostgresCompatConnection:
    pool = _connection_pool()
    connection = pool.getconn(timeout=15)
    return PostgresCompatConnection(connection, pool, readonly=readonly)


__all__ = [
    "CompatRow",
    "PostgresCompatConnection",
    "connect_postgres",
    "postgres_enabled",
    "postgres_safe_target",
    "postgres_settings",
    "raw_postgres_connection",
    "split_sql_script",
    "translate_sql",
]
