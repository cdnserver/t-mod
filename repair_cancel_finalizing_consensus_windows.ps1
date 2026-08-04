param(
    [string]$DatabasePath = "C:\Users\Admin\Documents\SGLDiscordBot\data\tmod.db"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:DATABASE_FILE = $DatabasePath

$python = @'
import os
import sqlite3
import sys
from pathlib import Path

import storage

db = Path(os.environ.get("DATABASE_FILE", ""))
print(f"[repair] Database: {db}")
if not db.exists():
    print("[repair] Database file not found.", file=sys.stderr)
    raise SystemExit(2)

try:
    with storage.connect() as con:
        quick = con.execute("PRAGMA quick_check").fetchone()
        print(f"[repair] quick_check: {quick[0] if quick else 'unknown'}")
        rows = con.execute(
            """
            SELECT session_key, guild_id, stage, current_bill_id, updated_at
            FROM tvrs_consensus_sessions
            WHERE finished_at IS NULL AND stage = 'finalizing'
            ORDER BY updated_at DESC
            """
        ).fetchall()
except sqlite3.DatabaseError as exc:
    print(f"[repair] SQLite error before repair: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("[repair] Stop the bot/Docker container first. If this remains, restore the latest backup or run sqlite .recover.", file=sys.stderr)
    raise SystemExit(3)

if not rows:
    print("[repair] No active finalizing consensus sessions found.")
    raise SystemExit(0)

for row in rows:
    session_key = str(row["session_key"])
    print(
        "[repair] Quarantine finalizing session: "
        f"guild={row['guild_id']} session={session_key} bill={row['current_bill_id']}"
    )
    storage.tvrs_consensus_quarantine_session(
        session_key,
        "Manual emergency repair: finalizing consensus loop cancelled; rerun consensus manually.",
    )

print(f"[repair] Done. Cancelled sessions: {len(rows)}")
'@

Set-Location $scriptDir
$python | python -
exit $LASTEXITCODE
