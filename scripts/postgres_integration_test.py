#!/usr/bin/env python3
"""Disposable PostgreSQL contract test used by CI and release validation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]


def run() -> None:
    if os.getenv("TMOD_POSTGRES_INTEGRATION") != "1":
        raise RuntimeError("TMOD_POSTGRES_INTEGRATION=1 is required")
    admin_url = os.environ["DATABASE_URL"]
    database = f"tmod_ci_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    target_url = admin_url.rsplit("/", 1)[0] + f"/{database}"
    try:
        with tempfile.TemporaryDirectory(prefix="tmod-sqlite-") as directory:
            sqlite_path = Path(directory) / "tmod.db"
            sqlite_env = dict(os.environ)
            sqlite_env.pop("TMOD_DATABASE_BACKEND", None)
            sqlite_env.pop("DATABASE_URL", None)
            sqlite_env.update(
                DATA_DIR=directory,
                DATABASE_FILE=str(sqlite_path),
            )
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import storage; storage.init_db(); n=storage.utc_now_iso(); "
                        "c=storage.connect(); "
                        "c.execute(\"INSERT INTO members(guild_id,user_id,display_name,name,mention,is_bot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)\","
                        "(1488,7215,'Иван','ivan','<@7215>',0,n,n)); "
                        "c.execute(\"INSERT INTO member_profiles(guild_id,user_id,preferred_name,created_at,updated_at) VALUES(?,?,?,?,?)\","
                        "(1488,7215,'Иван',n,n)); c.commit(); c.close()"
                    ),
                ],
                cwd=ROOT,
                env=sqlite_env,
                check=True,
            )
            postgres_env = dict(os.environ)
            postgres_env.update(
                TMOD_DATABASE_BACKEND="postgresql",
                DATABASE_URL=target_url,
                DATA_DIR=directory,
                DATABASE_FILE=str(sqlite_path),
                TMOD_DB_BACKUP_DIR=str(Path(directory) / "backups"),
                TMOD_MIGRATION_BACKUP_REQUIRED="false",
            )
            subprocess.run(
                [sys.executable, "scripts/migrate_sqlite_to_postgres.py"],
                cwd=ROOT,
                env=postgres_env,
                check=True,
            )
            smoke = (
                "import sqlite3,storage; n=storage.utc_now_iso(); "
                "c=storage.connect(); c.execute('BEGIN IMMEDIATE'); "
                "x=c.execute(\"INSERT INTO profile_characters(guild_id,user_id,nickname,static_id,position,is_public,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)\","
                "(1488,7215,'Сол Гудман','263345',1,1,n,n)); assert x.lastrowid>0; "
                "b=c.executemany(\"INSERT OR IGNORE INTO web_section_grants(guild_id,user_id,section,granted_by_id,created_at) VALUES(?,?,?,?,?)\","
                "[(1488,7215,'minecraft',1,n),(1488,7215,'minecraft',1,n)]); assert b.rowcount==1; "
                "c.commit(); c.close(); "
                "r=storage.connect(); row=r.execute(\"SELECT nickname FROM profile_characters WHERE T_CASEFOLD(nickname) LIKE T_CASEFOLD(?)\",('%гудман%',)).fetchone(); "
                "assert row['nickname']=='Сол Гудман'; r.close()"
            )
            subprocess.run(
                [sys.executable, "-c", smoke],
                cwd=ROOT,
                env=postgres_env,
                check=True,
            )
            with psycopg.connect(target_url) as verification:
                member = verification.execute(
                    "SELECT display_name FROM members WHERE user_id = 7215"
                ).fetchone()
                marker = verification.execute(
                    "SELECT COUNT(*) FROM tmod_platform_migrations"
                ).fetchone()
                assert member and member[0] == "Иван"
                assert marker and marker[0] == 1
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (database,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))


if __name__ == "__main__":
    run()
    print("PostgreSQL integration: OK")
