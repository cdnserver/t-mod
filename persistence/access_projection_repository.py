"""Durable Discord access projection for web/API authorization.

Discord remains the source of truth for guild membership and roles.  This
table is the last successfully observed state, allowing HTTP services to keep
serving authenticated accounts while the Discord gateway reconnects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso
from persistence.activity_repository import set_meta


def _avatar_key(guild_id: int, user_id: int) -> str:
    return f"desktop_avatar:v1:{int(guild_id)}:{int(user_id)}"


def get_web_avatar_url(guild_id: int, user_id: int) -> str | None:
    """Cosmetic Discord identity, deliberately separate from authorization."""
    with connect_readonly() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (_avatar_key(guild_id, user_id),)).fetchone()
    return str(row["value"]) if row and row["value"] else None


@dataclass(frozen=True, slots=True)
class WebAccessProjection:
    guild_id: int
    user_id: int
    display_name: str
    guild_member: bool
    administrator: bool
    role_ids: tuple[int, ...]
    observed_at: str
    created_at: str
    updated_at: str


def _role_ids(value: object) -> tuple[int, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(
        sorted(
            {
                int(role_id)
                for role_id in decoded
                if str(role_id).strip().isdigit() and int(role_id) > 0
            }
        )
    )


def _normalized_role_ids(values: Iterable[object]) -> list[int]:
    normalized: set[int] = set()
    for value in values:
        try:
            role_id = int(value)
        except (TypeError, ValueError):
            continue
        if role_id > 0:
            normalized.add(role_id)
    return sorted(normalized)


def _projection(row: object | None) -> WebAccessProjection | None:
    if row is None:
        return None
    return WebAccessProjection(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        display_name=str(row["display_name"] or f"User {row['user_id']}"),
        guild_member=bool(row["guild_member"]),
        administrator=bool(row["administrator"]),
        role_ids=_role_ids(row["role_ids_json"]),
        observed_at=str(row["observed_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def upsert_web_access_projection(
    guild_id: int,
    user_id: int,
    display_name: str,
    *,
    administrator: bool,
    role_ids: Iterable[int] = (),
    observed_at: str | None = None,
    avatar_url: str | None = None,
) -> WebAccessProjection:
    now = observed_at or utc_now_iso()
    normalized_roles = _normalized_role_ids(role_ids)
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO web_access_projection(
                guild_id, user_id, display_name, guild_member, administrator,
                role_ids_json, observed_at, created_at, updated_at
            )
            VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                guild_member = 1,
                administrator = excluded.administrator,
                role_ids_json = excluded.role_ids_json,
                observed_at = excluded.observed_at,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                int(user_id),
                str(display_name or f"User {int(user_id)}"),
                1 if administrator else 0,
                json.dumps(normalized_roles, separators=(",", ":")),
                now,
                now,
                now,
            ),
        )
        if avatar_url is not None:
            set_meta(con, _avatar_key(guild_id, user_id), str(avatar_url)[:2048])
        con.commit()
    result = get_web_access_projection(guild_id, user_id)
    if result is None:  # pragma: no cover - protects against storage corruption
        raise RuntimeError("web_access_projection_write_failed")
    return result


def mark_web_access_projection_departed(
    guild_id: int,
    user_id: int,
    *,
    observed_at: str | None = None,
) -> bool:
    now = observed_at or utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE web_access_projection
            SET guild_member = 0,
                administrator = 0,
                role_ids_json = '[]',
                observed_at = ?,
                updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (now, now, int(guild_id), int(user_id)),
        )
        con.commit()
        return int(cursor.rowcount or 0) > 0


def replace_web_access_projections(
    guild_id: int,
    members: Sequence[Mapping[str, object]],
    *,
    observed_at: str | None = None,
    revoke_missing: bool = True,
) -> dict[str, int]:
    """Atomically replace one guild snapshot and revoke missing members."""

    now = observed_at or utc_now_iso()
    normalized: list[tuple[int, str, bool, list[int]]] = []
    for member in members:
        user_id = int(member.get("user_id") or 0)
        if user_id <= 0:
            continue
        roles = _normalized_role_ids(member.get("role_ids") or ())
        normalized.append(
            (
                user_id,
                str(member.get("display_name") or f"User {user_id}"),
                bool(member.get("administrator")),
                roles,
            )
        )
    active_ids = {item[0] for item in normalized}
    avatars = {int(member["user_id"]): str(member["avatar_url"] or "")[:2048]
               for member in members if int(member.get("user_id") or 0) > 0 and "avatar_url" in member}
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        for user_id, display_name, administrator, roles in normalized:
            con.execute(
                """
                INSERT INTO web_access_projection(
                    guild_id, user_id, display_name, guild_member, administrator,
                    role_ids_json, observed_at, created_at, updated_at
                )
                VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    guild_member = 1,
                    administrator = excluded.administrator,
                    role_ids_json = excluded.role_ids_json,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    int(guild_id),
                    user_id,
                    display_name,
                    1 if administrator else 0,
                    json.dumps(roles, separators=(",", ":")),
                    now,
                    now,
                    now,
                ),
            )
        revoked = 0
        for user_id, avatar_url in avatars.items():
            set_meta(con, _avatar_key(guild_id, user_id), avatar_url)
        if revoke_missing:
            rows = con.execute(
                """
                SELECT user_id FROM web_access_projection
                WHERE guild_id = ? AND guild_member = 1
                """,
                (int(guild_id),),
            ).fetchall()
            for row in rows:
                user_id = int(row["user_id"])
                if user_id in active_ids:
                    continue
                con.execute(
                    """
                    UPDATE web_access_projection
                    SET guild_member = 0, administrator = 0, role_ids_json = '[]',
                        observed_at = ?, updated_at = ?
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (now, now, int(guild_id), user_id),
                )
                revoked += 1
        con.commit()
    return {"active": len(active_ids), "revoked": revoked}


def get_web_access_projection(
    guild_id: int,
    user_id: int,
) -> WebAccessProjection | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM web_access_projection
            WHERE guild_id = ? AND user_id = ?
            """,
            (int(guild_id), int(user_id)),
        ).fetchone()
    return _projection(row)


__all__ = [
    "WebAccessProjection",
    "get_web_avatar_url",
    "get_web_access_projection",
    "mark_web_access_projection_departed",
    "replace_web_access_projections",
    "upsert_web_access_projection",
]
