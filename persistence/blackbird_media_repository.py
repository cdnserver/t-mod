"""Opt-in public profiles and a small, auditable Blackbird social feed."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from io import BytesIO
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


COVER_THEMES = frozenset({"orbit", "night", "silver"})
ASSET_KINDS = frozenset({"avatar", "cover"})
MAX_ASSET_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_ASSET_STORED_BYTES = 2 * 1024 * 1024
MAX_DATABASE_ID = 9223372036854775807


def _identifier(value: int, error: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_DATABASE_ID:
        raise ValueError(error)
    return value


def _prepared_asset(kind: str, payload: bytes) -> bytes:
    if kind not in ASSET_KINDS or not payload or len(payload) > MAX_ASSET_UPLOAD_BYTES:
        raise ValueError("media_asset_invalid")
    try:
        with Image.open(BytesIO(payload)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                raise ValueError("media_asset_type_invalid")
            width, height = source.size
            if width < 96 or height < 96 or width > 8192 or height > 8192 or width * height > 16_000_000:
                raise ValueError("media_asset_dimensions_invalid")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
            size = (512, 512) if kind == "avatar" else (1600, 560)
            resized = ImageOps.fit(oriented.convert(mode), size, method=Image.Resampling.LANCZOS)
            output = BytesIO()
            resized.save(output, format="WEBP", quality=82, method=4)
            content = output.getvalue()
            if not content or len(content) > MAX_ASSET_STORED_BYTES:
                raise ValueError("media_asset_size_invalid")
            return content
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("media_asset_type_invalid") from exc


def save_asset(guild_id: int, user_id: int, kind: str, payload: bytes) -> str:
    content = _prepared_asset(kind, payload)
    revision = sha256(content).hexdigest()[:20]
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        account = con.execute(
            "SELECT login_display,login_key FROM web_credentials WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        if not account:
            raise ValueError("media_account_unavailable")
        con.execute(
            """INSERT INTO blackbird_media_profiles
               (guild_id,user_id,display_name,search_key,bio,cover_theme,is_public,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO NOTHING""",
            (guild_id, user_id, account["login_display"], account["login_key"], "", "orbit", 0, now, now),
        )
        con.execute(
            """INSERT INTO blackbird_media_assets
               (guild_id,user_id,kind,mime_type,content,byte_size,revision,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,user_id,kind) DO UPDATE SET
               mime_type=excluded.mime_type,content=excluded.content,byte_size=excluded.byte_size,
               revision=excluded.revision,updated_at=excluded.updated_at""",
            (guild_id, user_id, kind, "image/webp", content, len(content), revision, now),
        )
        con.commit()
    return revision


def read_asset(guild_id: int, viewer_id: int, user_id: int, kind: str) -> tuple[str, bytes, str] | None:
    if kind not in ASSET_KINDS or type(user_id) is not int or not 0 < user_id <= MAX_DATABASE_ID:
        return None
    with connect_readonly() as con:
        row = con.execute(
            """SELECT a.mime_type,a.content,a.byte_size,a.revision
               FROM blackbird_media_assets a
               JOIN blackbird_media_profiles p ON p.guild_id=a.guild_id AND p.user_id=a.user_id
               WHERE a.guild_id=? AND a.user_id=? AND a.kind=? AND (p.is_public=1 OR a.user_id=?)""",
            (guild_id, user_id, kind, viewer_id),
        ).fetchone()
    if row is None:
        return None
    content = bytes(row["content"])
    if len(content) != int(row["byte_size"]):
        raise ValueError("media_asset_corrupted")
    return str(row["mime_type"]), content, str(row["revision"])


def delete_asset(guild_id: int, user_id: int, kind: str) -> bool:
    if kind not in ASSET_KINDS:
        raise ValueError("media_asset_invalid")
    with _db_lock, connect() as con:
        result = con.execute(
            "DELETE FROM blackbird_media_assets WHERE guild_id=? AND user_id=? AND kind=?",
            (guild_id, user_id, kind),
        )
        con.commit()
    return bool(result.rowcount)


def _profile(row: Any) -> dict[str, Any]:
    result = dict(row)
    result.pop("search_key", None)
    result["user_id"] = str(result["user_id"])
    result["is_public"] = bool(result["is_public"])
    return result


def own_profile(guild_id: int, user_id: int, fallback_name: str) -> dict[str, Any]:
    with connect_readonly() as con:
        row = con.execute(
            """SELECT p.*,a.revision AS avatar_revision,c.revision AS cover_revision
               FROM blackbird_media_profiles p
               LEFT JOIN blackbird_media_assets a ON a.guild_id=p.guild_id AND a.user_id=p.user_id AND a.kind='avatar'
               LEFT JOIN blackbird_media_assets c ON c.guild_id=p.guild_id AND c.user_id=p.user_id AND c.kind='cover'
               WHERE p.guild_id=? AND p.user_id=?""",
            (guild_id, user_id),
        ).fetchone()
    if row:
        return _profile(row)
    return {
        "user_id": str(user_id), "display_name": str(fallback_name)[:64],
        "bio": "", "cover_theme": "orbit", "is_public": False,
        "avatar_revision": None, "cover_revision": None,
        "created_at": "", "updated_at": "",
    }


def save_profile(guild_id: int, user_id: int, name: str, bio: str, cover_theme: str, is_public: bool) -> dict[str, Any]:
    name = str(name or "").strip()
    bio = str(bio or "").strip()
    if not (2 <= len(name) <= 64) or len(bio) > 500 or any(ord(char) < 32 and char not in "\n\t" for char in name + bio):
        raise ValueError("media_profile_invalid")
    if cover_theme not in COVER_THEMES or type(is_public) is not bool:
        raise ValueError("media_profile_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        if not con.execute("SELECT 1 FROM web_credentials WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone():
            raise ValueError("media_account_unavailable")
        con.execute(
            """INSERT INTO blackbird_media_profiles
               (guild_id,user_id,display_name,search_key,bio,cover_theme,is_public,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET
               display_name=excluded.display_name,search_key=excluded.search_key,bio=excluded.bio,
               cover_theme=excluded.cover_theme,is_public=excluded.is_public,
               updated_at=excluded.updated_at""",
            (guild_id, user_id, name, name.casefold(), bio, cover_theme, int(is_public), now, now),
        )
        con.commit()
    return own_profile(guild_id, user_id, name)


def public_profile(guild_id: int, viewer_id: int, user_id: int) -> dict[str, Any] | None:
    _identifier(user_id, "media_profile_invalid")
    if user_id == viewer_id:
        return None
    with connect_readonly() as con:
        row = con.execute(
            """SELECT p.*,a.revision AS avatar_revision,c.revision AS cover_revision
               FROM blackbird_media_profiles p
               LEFT JOIN blackbird_media_assets a ON a.guild_id=p.guild_id AND a.user_id=p.user_id AND a.kind='avatar'
               LEFT JOIN blackbird_media_assets c ON c.guild_id=p.guild_id AND c.user_id=p.user_id AND c.kind='cover'
               WHERE p.guild_id=? AND p.user_id=? AND p.is_public=1""",
            (guild_id, user_id),
        ).fetchone()
    return _profile(row) if row else None


def search_profiles(guild_id: int, viewer_id: int, query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = str(query or "").strip().casefold()
    if not (2 <= len(query) <= 64):
        raise ValueError("media_search_invalid")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with connect_readonly() as con:
        rows = con.execute(
            """SELECT p.*,a.revision AS avatar_revision,c.revision AS cover_revision FROM blackbird_media_profiles p
               JOIN web_credentials w ON w.guild_id=p.guild_id AND w.user_id=p.user_id
               LEFT JOIN blackbird_media_assets a ON a.guild_id=p.guild_id AND a.user_id=p.user_id AND a.kind='avatar'
               LEFT JOIN blackbird_media_assets c ON c.guild_id=p.guild_id AND c.user_id=p.user_id AND c.kind='cover'
               WHERE p.guild_id=? AND p.is_public=1 AND p.user_id!=?
                 AND (p.search_key LIKE ? ESCAPE '\\' OR w.login_key LIKE ? ESCAPE '\\')
               ORDER BY p.updated_at DESC LIMIT ?""",
            (guild_id, viewer_id, f"%{escaped}%", f"%{escaped}%", max(1, min(limit, 40))),
        ).fetchall()
    return [_profile(row) for row in rows]


def _post(row: Any) -> dict[str, Any]:
    result = dict(row)
    result.pop("client_nonce", None)
    result.pop("client_payload_hash", None)
    result["author_id"] = str(result["author_id"])
    return result


def feed(guild_id: int, viewer_id: int, *, kind: str = "all", before_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    if kind not in {"all", "post", "rollback"}:
        raise ValueError("media_kind_invalid")
    if before_id is not None:
        _identifier(before_id, "media_cursor_invalid")
    with connect_readonly() as con:
        rows = con.execute(
            """SELECT m.id,m.author_id,m.kind,m.body,m.source_url,m.created_at,p.display_name,p.cover_theme,
                      a.revision AS avatar_revision
               FROM blackbird_media_posts m
               JOIN blackbird_media_profiles p ON p.guild_id=m.guild_id AND p.user_id=m.author_id
               LEFT JOIN blackbird_media_assets a ON a.guild_id=m.guild_id AND a.user_id=m.author_id AND a.kind='avatar'
               WHERE m.guild_id=? AND m.deleted_at IS NULL AND p.is_public=1
                 AND (?='all' OR m.kind=?) AND (? IS NULL OR m.id<?)
               ORDER BY m.id DESC LIMIT ?""",
            (guild_id, kind, kind, before_id, before_id, max(1, min(limit, 40))),
        ).fetchall()
    return [_post(row) for row in rows]


def profile_posts(guild_id: int, viewer_id: int, author_id: int, *, before_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """Return a person's posts only while their profile remains public.

    The visibility predicate lives in this query, not just in the profile
    lookup performed by the UI, so an old open window cannot read posts after
    the owner makes the profile private.
    """
    _identifier(author_id, "media_profile_invalid")
    if before_id is not None:
        _identifier(before_id, "media_cursor_invalid")
    with connect_readonly() as con:
        rows = con.execute(
            """SELECT m.id,m.author_id,m.kind,m.body,m.source_url,m.created_at,p.display_name,p.cover_theme,
                      a.revision AS avatar_revision
               FROM blackbird_media_posts m
               JOIN blackbird_media_profiles p ON p.guild_id=m.guild_id AND p.user_id=m.author_id
               LEFT JOIN blackbird_media_assets a ON a.guild_id=m.guild_id AND a.user_id=m.author_id AND a.kind='avatar'
               WHERE m.guild_id=? AND m.author_id=? AND m.deleted_at IS NULL
                 AND (p.is_public=1 OR m.author_id=?) AND (? IS NULL OR m.id<?)
               ORDER BY m.id DESC LIMIT ?""",
            (guild_id, author_id, viewer_id, before_id, before_id, max(1, min(limit, 40))),
        ).fetchall()
    return [_post(row) for row in rows]


def create_post(guild_id: int, user_id: int, kind: str, body: str, source_url: str = "", *, client_nonce: str = "") -> dict[str, Any]:
    kind = str(kind or "")
    body = str(body or "").strip()
    source_url = str(source_url or "").strip()
    if kind not in {"post", "rollback"} or not (3 <= len(body) <= 2000):
        raise ValueError("media_post_invalid")
    if any(ord(char) < 32 and char not in "\n\t" for char in body):
        raise ValueError("media_post_invalid")
    if source_url:
        try:
            parsed = urlparse(source_url)
            # Accessing .port also validates malformed/out-of-range ports.
            port = parsed.port
        except ValueError:
            raise ValueError("media_source_invalid") from None
        if len(source_url) > 2048 or any(char.isspace() or ord(char) < 32 for char in source_url) or parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or port == 0:
            raise ValueError("media_source_invalid")
    if kind == "rollback" and not source_url:
        raise ValueError("media_source_required")
    nonce = str(client_nonce or "")
    if nonce and not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise ValueError("media_request_invalid")
    payload_hash = sha256(json.dumps([kind, body, source_url], ensure_ascii=False).encode("utf-8")).hexdigest()
    now = utc_now_iso()
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        profile = con.execute(
            "SELECT display_name,cover_theme FROM blackbird_media_profiles WHERE guild_id=? AND user_id=? AND is_public=1",
            (guild_id, user_id),
        ).fetchone()
        if not profile:
            raise ValueError("media_profile_not_public")
        def existing_request() -> dict[str, Any] | None:
            if not nonce:
                return None
            row = con.execute(
                "SELECT * FROM blackbird_media_posts WHERE guild_id=? AND author_id=? AND client_nonce=?",
                (guild_id, user_id, nonce),
            ).fetchone()
            if row is None:
                return None
            if row["client_payload_hash"] != payload_hash:
                raise ValueError("media_request_conflict")
            if row["deleted_at"] is not None:
                raise ValueError("media_post_deleted")
            return {**_post(row), **dict(profile)}

        existing = existing_request()
        if existing is not None:
            return existing
        count = con.execute(
            "SELECT COUNT(*) AS n FROM blackbird_media_posts WHERE guild_id=? AND author_id=? AND created_at>=?",
            (guild_id, user_id, since),
        ).fetchone()
        if int(count["n"]) >= 6:
            raise ValueError("media_rate_limited")
        cursor = con.execute(
            """INSERT INTO blackbird_media_posts(guild_id,author_id,kind,body,source_url,created_at,client_nonce,client_payload_hash)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,author_id,client_nonce) DO NOTHING""",
            (guild_id, user_id, kind, body, source_url, now, nonce or None, payload_hash if nonce else None),
        )
        if not cursor.rowcount:
            existing = existing_request()
            if existing is None:
                raise ValueError("media_request_unavailable")
            con.commit()
            return existing
        con.commit()
    return {
        "id": int(cursor.lastrowid), "author_id": str(user_id), "kind": kind,
        "body": body, "source_url": source_url, "created_at": now,
        "display_name": profile["display_name"], "cover_theme": profile["cover_theme"],
    }


def delete_post(guild_id: int, user_id: int, post_id: int) -> bool:
    _identifier(post_id, "media_post_invalid")
    with _db_lock, connect() as con:
        result = con.execute(
            "UPDATE blackbird_media_posts SET deleted_at=? WHERE guild_id=? AND id=? AND author_id=? AND deleted_at IS NULL",
            (utc_now_iso(), guild_id, post_id, user_id),
        )
        con.commit()
    return bool(result.rowcount)
