"""Durable optimistic-concurrency storage for Reactor board games."""

from __future__ import annotations

import json
import secrets
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


class GameStorageError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _project(row: Any) -> dict[str, Any]:
    item = dict(row)
    try:
        item["state"] = json.loads(str(item.pop("state_json")))
    except (TypeError, ValueError, json.JSONDecodeError):
        item["state"] = {}
        item.pop("state_json", None)
    return item


def game_create(
    *,
    guild_id: int,
    game_type: str,
    mode: str,
    host_user_id: int,
    host_display: str,
    host_side: str,
    bot_level: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    if game_type not in {"chess", "backgammon"} or mode not in {"bot", "friend"}:
        raise GameStorageError("game_settings_invalid")
    if host_side not in {"white", "black"}:
        raise GameStorageError("game_side_invalid")
    now = utc_now_iso()
    match_id = secrets.token_urlsafe(12).replace("-", "").replace("_", "")
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO game_matches(
                id, guild_id, game_type, mode, host_user_id, host_display,
                host_side, bot_level, status, turn_side, state_json,
                last_action_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'white', ?, ?, ?, ?)
            """,
            (
                match_id,
                int(guild_id),
                game_type,
                mode,
                int(host_user_id),
                str(host_display or "Игрок")[:100],
                host_side,
                max(1, min(3, int(bot_level))),
                "active" if mode == "bot" else "waiting",
                _json(state),
                now,
                now,
                now,
            ),
        )
        con.execute(
            "INSERT INTO game_match_events(match_id, actor_user_id, action, payload_json, created_at) VALUES(?, ?, 'created', ?, ?)",
            (match_id, int(host_user_id), _json({"mode": mode, "side": host_side}), now),
        )
        row = con.execute("SELECT * FROM game_matches WHERE id = ?", (match_id,)).fetchone()
        con.commit()
    return _project(row)


def game_get(guild_id: int, match_id: str) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            "SELECT * FROM game_matches WHERE guild_id = ? AND id = ?",
            (int(guild_id), str(match_id)),
        ).fetchone()
    return _project(row) if row is not None else None


def game_list_for_user(guild_id: int, user_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM game_matches
            WHERE guild_id = ? AND (host_user_id = ? OR guest_user_id = ?)
            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                     updated_at DESC LIMIT ?
            """,
            (int(guild_id), int(user_id), int(user_id), max(1, min(50, int(limit)))),
        ).fetchall()
    return [_project(row) for row in rows]


def game_join(guild_id: int, match_id: str, user_id: int, display_name: str) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM game_matches WHERE guild_id = ? AND id = ?",
            (int(guild_id), str(match_id)),
        ).fetchone()
        if row is None:
            raise GameStorageError("game_missing")
        if int(row["host_user_id"]) == int(user_id):
            con.rollback()
            return _project(row)
        if str(row["mode"]) != "friend" or str(row["status"]) != "waiting" or row["guest_user_id"] is not None:
            raise GameStorageError("game_not_joinable")
        con.execute(
            """
            UPDATE game_matches
            SET guest_user_id = ?, guest_display = ?, status = 'active',
                version = version + 1, last_action_at = ?, updated_at = ?
            WHERE id = ? AND status = 'waiting' AND guest_user_id IS NULL
            """,
            (int(user_id), str(display_name or "Игрок")[:100], now, now, str(match_id)),
        )
        con.execute(
            "INSERT INTO game_match_events(match_id, actor_user_id, action, payload_json, created_at) VALUES(?, ?, 'joined', '{}', ?)",
            (str(match_id), int(user_id), now),
        )
        updated = con.execute("SELECT * FROM game_matches WHERE id = ?", (str(match_id),)).fetchone()
        con.commit()
    return _project(updated)


def game_update(
    guild_id: int,
    match_id: str,
    *,
    expected_version: int,
    actor_user_id: int | None,
    action: str,
    state: dict[str, Any],
    turn_side: str,
    status: str,
    result: str | None = None,
    winner_side: str | None = None,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            UPDATE game_matches
            SET state_json = ?, turn_side = ?, status = ?, result = ?, winner_side = ?,
                version = version + 1, last_action_at = ?, updated_at = ?
            WHERE guild_id = ? AND id = ? AND version = ?
            """,
            (
                _json(state),
                turn_side,
                status,
                result,
                winner_side,
                now,
                now,
                int(guild_id),
                str(match_id),
                int(expected_version),
            ),
        )
        if cursor.rowcount != 1:
            raise GameStorageError("game_version_conflict")
        con.execute(
            "INSERT INTO game_match_events(match_id, actor_user_id, action, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (str(match_id), int(actor_user_id) if actor_user_id else None, str(action)[:40], _json(event or {}), now),
        )
        row = con.execute("SELECT * FROM game_matches WHERE id = ?", (str(match_id),)).fetchone()
        con.commit()
    return _project(row)


__all__ = [
    "GameStorageError",
    "game_create",
    "game_get",
    "game_join",
    "game_list_for_user",
    "game_update",
]
