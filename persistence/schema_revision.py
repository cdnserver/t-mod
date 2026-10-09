"""Revision gate for PostgreSQL startup DDL, committed with the schema itself."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path
from typing import Any

MARKER = "schema:postgres-source-revision:v1"


def schema_revision() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    # Include helpers which change generated DDL, not the release commit: an
    # ordinary UI/bot update must not trigger another full schema transaction.
    for name in ("schema.py", "core.py", "postgres_compat.py", "schema_revision.py"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def postgres_schema_current(con: Any, revision: str) -> bool:
    if con.execute("SELECT to_regclass('public.meta')").fetchone()[0] is None:
        return False
    row = con.execute("SELECT value FROM meta WHERE key = ?", (MARKER,)).fetchone()
    return bool(row is not None and row[0] == revision)


def workspace_open_index_current(con: Any, *, postgres: bool) -> bool:
    """Inspect the actual index; don't acquire a DDL lock just to recreate it."""
    if postgres:
        row = con.execute(
            """SELECT i.indisunique, i.indisvalid,
                      pg_get_indexdef(i.indexrelid, 1, true) AS first_column,
                      pg_get_indexdef(i.indexrelid, 2, true) AS second_column,
                      i.indnkeyatts, pg_get_expr(i.indpred, i.indrelid) AS predicate
               FROM pg_index i
               WHERE i.indexrelid = to_regclass('public.idx_tvrs_bill_workspaces_one_open')
                 AND i.indrelid = to_regclass('public.tvrs_bill_workspaces')"""
        ).fetchone()
        if not row or not row[0] or not row[1] or row[2] != "guild_id" or row[3] != "author_id" or row[4] != 2:
            return False
        predicate = str(row[5] or "").lower().replace("::text", "")
        predicate = re.sub(r"[\s()]", "", predicate)
        return predicate in {"status=anyarray['draft','review']", "statusin'draft','review'"}
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_tvrs_bill_workspaces_one_open'").fetchone()
    if not row:
        return False
    statement = re.sub(r"\s+", "", str(row[0]).lower()).rstrip(";")
    return bool(re.fullmatch(r"createuniqueindex(?:ifnotexists)?idx_tvrs_bill_workspaces_one_openontvrs_bill_workspaces\(guild_id,author_id\)wherestatusin\('draft','review'\)", statement))
