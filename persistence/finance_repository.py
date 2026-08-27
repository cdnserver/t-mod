from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso
from persistence.activity_repository import _record_bot_action

FINANCE_SNAPSHOT_KINDS = {"daily", "interim"}
FINANCE_MOVEMENT_KINDS = {"deposit", "withdraw"}


def _finance_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def finance_get_or_create_daily_prompt(*, guild_id: int, report_date: str, channel_id: int) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO finance_daily_prompts(
                guild_id, report_date, channel_id, message_id, status,
                event_id, created_at, updated_at
            )
            VALUES(?, ?, ?, NULL, 'open', NULL, ?, ?)
            ON CONFLICT(guild_id, report_date) DO UPDATE SET
                channel_id = excluded.channel_id,
                updated_at = excluded.updated_at
            """,
            (guild_id, report_date, channel_id, now, now),
        )
        row = con.execute(
            "SELECT * FROM finance_daily_prompts WHERE guild_id = ? AND report_date = ?",
            (guild_id, report_date),
        ).fetchone()
        con.commit()
    prompt = _finance_row(row)
    if prompt is None:
        raise RuntimeError("finance_prompt_create_failed")
    return prompt


def finance_get_daily_prompt(prompt_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM finance_daily_prompts WHERE id = ?", (prompt_id,)).fetchone()
    return _finance_row(row)


def finance_get_daily_prompt_by_message(*, guild_id: int, message_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM finance_daily_prompts WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        ).fetchone()
    return _finance_row(row)


def finance_bind_daily_prompt_message(*, prompt_id: int, channel_id: int, message_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE finance_daily_prompts
            SET channel_id = ?, message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (channel_id, message_id, now, prompt_id),
        )
        row = con.execute("SELECT * FROM finance_daily_prompts WHERE id = ?", (prompt_id,)).fetchone()
        con.commit()
    return _finance_row(row)


def finance_recent_daily_prompts(*, guild_id: int, limit: int = 14) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT * FROM finance_daily_prompts WHERE guild_id = ? ORDER BY report_date DESC LIMIT ?",
            (guild_id, max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(row) for row in rows]


def _finance_enqueue_notifications(
    con: sqlite3.Connection,
    *,
    event_id: int,
    log_channel_id: int,
    admin_user_id: int,
    now: str,
) -> None:
    destinations = []
    if int(log_channel_id) > 0:
        destinations.append(("log_channel", int(log_channel_id)))
    if int(admin_user_id) > 0:
        destinations.append(("admin_dm", int(admin_user_id)))
    for destination_kind, destination_id in destinations:
        con.execute(
            """
            INSERT INTO finance_notifications(
                event_id, destination_kind, destination_id, status, attempts,
                next_attempt_at, message_id, last_error, created_at, updated_at
            )
            VALUES(?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)
            ON CONFLICT(event_id, destination_kind, destination_id) DO NOTHING
            """,
            (event_id, destination_kind, destination_id, now, now, now),
        )


def finance_record_snapshot(
    *,
    guild_id: int,
    event_kind: str,
    amount: int,
    actor_id: int,
    actor_display: str | None,
    channel_id: int | None,
    message_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
    report_date: str | None = None,
    prompt_id: int | None = None,
) -> dict[str, Any]:
    if event_kind not in FINANCE_SNAPSHOT_KINDS:
        raise ValueError("finance_bad_snapshot_kind")
    if int(amount) < 0:
        raise ValueError("finance_bad_amount")
    if event_kind == "daily" and prompt_id is None:
        raise ValueError("finance_daily_prompt_required")

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        prompt = None
        if prompt_id is not None:
            prompt = con.execute(
                "SELECT * FROM finance_daily_prompts WHERE id = ? AND guild_id = ?",
                (prompt_id, guild_id),
            ).fetchone()
            if prompt is None:
                con.rollback()
                raise ValueError("finance_prompt_not_found")
            if str(prompt["status"]) != "open":
                existing = con.execute("SELECT * FROM finance_events WHERE prompt_id = ?", (prompt_id,)).fetchone()
                con.commit()
                if existing is None:
                    raise RuntimeError("finance_filled_prompt_without_event")
                result = dict(existing)
                result["already_recorded"] = True
                return result
            report_date = str(prompt["report_date"])
            channel_id = int(prompt["channel_id"])
            message_id = prompt["message_id"]

        cur = con.execute(
            """
            INSERT INTO finance_events(
                guild_id, event_kind, report_date, prompt_id, amount, delta,
                balance_before, balance_after, reason, captcha_digest, game_code,
                actor_id, actor_display, channel_id, message_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                event_kind,
                report_date,
                prompt_id,
                int(amount),
                int(amount),
                actor_id,
                actor_display,
                channel_id,
                message_id,
                now,
            ),
        )
        event_id = int(cur.lastrowid)
        if prompt_id is not None:
            con.execute(
                """
                UPDATE finance_daily_prompts
                SET status = 'filled', event_id = ?, updated_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (event_id, now, prompt_id),
            )
        _finance_enqueue_notifications(
            con,
            event_id=event_id,
            log_channel_id=log_channel_id,
            admin_user_id=admin_user_id,
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="finance",
            action_kind=f"finance_{event_kind}",
            target_type="finance_event",
            target_id=event_id,
            summary=(
                f"{'Ежедневный отчёт' if event_kind == 'daily' else 'Межотчёт'} казны: {int(amount)}"
            ),
            payload={"finance_event_id": event_id},
            now=now,
        )
        row = con.execute("SELECT * FROM finance_events WHERE id = ?", (event_id,)).fetchone()
        con.commit()
    event = _finance_row(row)
    if event is None:
        raise RuntimeError("finance_snapshot_create_failed")
    event["already_recorded"] = False
    event["action_id"] = action_id
    return event


def finance_record_movement(
    *,
    guild_id: int,
    event_kind: str,
    amount: int,
    reason: str,
    captcha_digest: str,
    game_code: str,
    actor_id: int,
    actor_display: str | None,
    channel_id: int | None,
    message_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
) -> dict[str, Any]:
    if event_kind not in FINANCE_MOVEMENT_KINDS:
        raise ValueError("finance_bad_movement_kind")
    if int(amount) <= 0:
        raise ValueError("finance_bad_amount")
    reason = str(reason or "").strip()
    if len(reason) < 10:
        raise ValueError("finance_reason_too_short")
    game_code = str(game_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{4}", game_code):
        raise ValueError("finance_bad_game_code")

    now = utc_now_iso()
    delta = int(amount) if event_kind == "deposit" else -int(amount)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        latest = con.execute(
            "SELECT balance_after FROM finance_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        balance_before = None if latest is None or latest["balance_after"] is None else int(latest["balance_after"])
        balance_after = None if balance_before is None else balance_before + delta
        cur = con.execute(
            """
            INSERT INTO finance_events(
                guild_id, event_kind, report_date, prompt_id, amount, delta,
                balance_before, balance_after, reason, captcha_digest, game_code,
                actor_id, actor_display, channel_id, message_id, created_at
            )
            VALUES(?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                event_kind,
                int(amount),
                delta,
                balance_before,
                balance_after,
                reason,
                captcha_digest,
                game_code,
                actor_id,
                actor_display,
                channel_id,
                message_id,
                now,
            ),
        )
        event_id = int(cur.lastrowid)
        _finance_enqueue_notifications(
            con,
            event_id=event_id,
            log_channel_id=log_channel_id,
            admin_user_id=admin_user_id,
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="finance",
            action_kind=f"finance_{event_kind}",
            target_type="finance_event",
            target_id=event_id,
            summary=f"{'Пополнение' if event_kind == 'deposit' else 'Снятие'} казны: {int(amount)} · код {game_code}",
            payload={"finance_event_id": event_id},
            now=now,
        )
        row = con.execute("SELECT * FROM finance_events WHERE id = ?", (event_id,)).fetchone()
        con.commit()
    event = _finance_row(row)
    if event is None:
        raise RuntimeError("finance_movement_create_failed")
    event["action_id"] = action_id
    return event


def finance_get_event(event_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM finance_events WHERE id = ?", (event_id,)).fetchone()
        result = _finance_row(row)
        if result is not None and result.get("reversed_event_id") is not None:
            reversed_row = con.execute(
                "SELECT * FROM finance_events WHERE id = ?",
                (int(result["reversed_event_id"]),),
            ).fetchone()
            result["reversed_event"] = _finance_row(reversed_row)
    return result


def _finance_replay_balance(rows: Iterable[sqlite3.Row], *, omit_event_id: int | None = None) -> int | None:
    balance: int | None = None
    for row in rows:
        event_id = int(row["id"])
        if omit_event_id is not None and event_id == omit_event_id:
            continue
        kind = str(row["event_kind"] or "")
        if kind in FINANCE_SNAPSHOT_KINDS:
            balance = int(row["amount"])
        elif kind in FINANCE_MOVEMENT_KINDS and balance is not None:
            balance += int(row["delta"] or 0)
    return balance


def finance_undo_last_action(
    *,
    guild_id: int,
    target_actor_id: int,
    undone_by_id: int,
    undone_by_display: str | None,
    channel_id: int | None,
    message_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
    target_event_id: int | None = None,
) -> dict[str, Any] | None:
    """Audit and reverse the target user's latest active finance action.

    Original rows are never deleted. An `undo` row points at the reversed event and
    stores the counterfactual current balance after replaying the active ledger.
    """
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if target_event_id is None:
            target = con.execute(
                """
                SELECT e.* FROM finance_events AS e
                WHERE e.guild_id = ?
                  AND e.actor_id = ?
                  AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u
                      WHERE u.reversed_event_id = e.id
                  )
                ORDER BY e.id DESC
                LIMIT 1
                """,
                (guild_id, target_actor_id),
            ).fetchone()
        else:
            target = con.execute(
                """
                SELECT e.* FROM finance_events AS e
                WHERE e.guild_id = ? AND e.id = ?
                  AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u
                      WHERE u.reversed_event_id = e.id
                  )
                """,
                (guild_id, int(target_event_id)),
            ).fetchone()
        if target is None:
            con.commit()
            return None
        craft_purchase = con.execute(
            "SELECT id FROM craft_purchases WHERE finance_event_id = ? AND undone_at IS NULL LIMIT 1",
            (int(target["id"]),),
        ).fetchone()
        if str(target["captcha_digest"] or "").startswith("craft:") or craft_purchase is not None:
            con.rollback()
            raise ValueError("finance_action_locked_by_craft")

        active_rows = con.execute(
            """
            SELECT e.* FROM finance_events AS e
            WHERE e.guild_id = ?
              AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
              AND NOT EXISTS (
                  SELECT 1 FROM finance_events AS u
                  WHERE u.reversed_event_id = e.id
              )
            ORDER BY e.id ASC
            """,
            (guild_id,),
        ).fetchall()
        target_id = int(target["id"])
        balance_before = _finance_replay_balance(active_rows)
        balance_after = _finance_replay_balance(active_rows, omit_event_id=target_id)
        if balance_before is not None and balance_after is not None:
            undo_delta = balance_after - balance_before
        else:
            undo_delta = None

        kind_labels = {
            "daily": "ежедневного отчёта",
            "interim": "межотчёта",
            "deposit": "пополнения",
            "withdraw": "снятия",
        }
        reason = f"Отмена {kind_labels.get(str(target['event_kind']), 'финансового действия')} #{target_id}"
        cur = con.execute(
            """
            INSERT INTO finance_events(
                guild_id, event_kind, report_date, prompt_id, reversed_event_id,
                amount, delta, balance_before, balance_after, reason,
                captcha_digest, game_code, actor_id, actor_display,
                channel_id, message_id, created_at
            )
            VALUES(?, 'undo', NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                target_id,
                int(target["amount"]),
                undo_delta,
                balance_before,
                balance_after,
                reason,
                undone_by_id,
                undone_by_display,
                channel_id,
                message_id,
                now,
            ),
        )
        undo_id = int(cur.lastrowid)

        prompt = None
        if str(target["event_kind"]) == "daily" and target["prompt_id"] is not None:
            # Free the prompt's unique slot so the reopened daily report can be
            # filled again. The audit relationship is retained by reversed_event_id.
            con.execute("UPDATE finance_events SET prompt_id = NULL WHERE id = ?", (target_id,))
            con.execute(
                """
                UPDATE finance_daily_prompts
                SET status = 'open', event_id = NULL, updated_at = ?
                WHERE id = ? AND event_id = ?
                """,
                (now, int(target["prompt_id"]), target_id),
            )
            prompt = con.execute(
                "SELECT * FROM finance_daily_prompts WHERE id = ?",
                (int(target["prompt_id"]),),
            ).fetchone()

        _finance_enqueue_notifications(
            con,
            event_id=undo_id,
            log_channel_id=log_channel_id,
            admin_user_id=admin_user_id,
            now=now,
        )
        con.execute(
            """
            UPDATE bot_actions
            SET status = 'undone', undone_by_id = ?, undone_by_display = ?,
                undone_at = ?, undo_reason = ?
            WHERE guild_id = ? AND target_type = 'finance_event' AND target_id = ?
              AND status = 'active'
            """,
            (
                undone_by_id,
                undone_by_display,
                now,
                reason,
                guild_id,
                str(target_id),
            ),
        )
        undo_row = con.execute("SELECT * FROM finance_events WHERE id = ?", (undo_id,)).fetchone()
        con.commit()

    return {
        "undo_event": _finance_row(undo_row),
        "reversed_event": _finance_row(target),
        "prompt": _finance_row(prompt),
    }


def finance_get_latest_state(guild_id: int) -> dict[str, Any]:
    with connect_readonly() as con:
        latest = con.execute(
            "SELECT * FROM finance_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        report = con.execute(
            """
            SELECT e.* FROM finance_events AS e
            WHERE e.guild_id = ?
              AND e.event_kind IN ('daily', 'interim')
              AND NOT EXISTS (
                  SELECT 1 FROM finance_events AS u
                  WHERE u.reversed_event_id = e.id
              )
            ORDER BY e.id DESC LIMIT 1
            """,
            (guild_id,),
        ).fetchone()
        movement_count = 0
        if report is not None:
            row = con.execute(
                """
                SELECT COUNT(*) AS n FROM finance_events AS e
                WHERE e.guild_id = ? AND e.id > ?
                  AND e.event_kind IN ('deposit', 'withdraw')
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u
                      WHERE u.reversed_event_id = e.id
                  )
                """,
                (guild_id, int(report["id"])),
            ).fetchone()
            movement_count = int(row["n"] or 0)
    return {
        "latest_event": _finance_row(latest),
        "latest_report": _finance_row(report),
        "estimated_balance": (None if latest is None else latest["balance_after"]),
        "movements_after_report": movement_count,
    }


def finance_search_events(
    guild_id: int,
    *,
    code: str | None = None,
    event_kind: str | None = None,
    actor_id: int | None = None,
    days: int | None = None,
    text: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    clauses = ["e.guild_id = ?"]
    params: list[Any] = [guild_id]
    if code:
        clean_code = re.sub(r"[^A-Za-z]", "", str(code)).upper()
        if len(clean_code) != 4:
            raise ValueError("finance_bad_game_code")
        clauses.append("e.game_code = ?")
        params.append(clean_code)
    if event_kind:
        allowed = FINANCE_SNAPSHOT_KINDS | FINANCE_MOVEMENT_KINDS | {"undo"}
        if event_kind not in allowed:
            raise ValueError("finance_bad_audit_kind")
        clauses.append("e.event_kind = ?")
        params.append(event_kind)
    if actor_id is not None:
        clauses.append("e.actor_id = ?")
        params.append(actor_id)
    if days is not None and int(days) > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min(int(days), 3650))).isoformat()
        clauses.append("e.created_at >= ?")
        params.append(cutoff)
    if text and str(text).strip():
        clauses.append("lower(COALESCE(e.reason, '')) LIKE ?")
        params.append(f"%{str(text).strip().lower()}%")
    params.append(max(1, min(int(limit), 100)))
    with _db_lock, connect() as con:
        rows = con.execute(
            f"""
            SELECT e.*,
                   EXISTS(SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id) AS is_undone,
                   (SELECT u.id FROM finance_events u WHERE u.reversed_event_id = e.id LIMIT 1) AS undo_event_id,
                   (SELECT p.id FROM craft_purchases p WHERE p.finance_event_id = e.id AND p.undone_at IS NULL LIMIT 1) AS craft_purchase_id,
                   (SELECT p.plan_id FROM craft_purchases p WHERE p.finance_event_id = e.id AND p.undone_at IS NULL LIMIT 1) AS craft_purchase_plan_id,
                   (SELECT b.id FROM craft_batches b WHERE b.finance_event_id = e.id AND b.undone_at IS NULL LIMIT 1) AS craft_batch_id,
                   (SELECT b.plan_id FROM craft_batches b WHERE b.finance_event_id = e.id AND b.undone_at IS NULL LIMIT 1) AS craft_batch_plan_id,
                   (SELECT a.id FROM bot_actions a WHERE a.target_type = 'finance_event' AND a.target_id = CAST(e.id AS TEXT) ORDER BY a.id DESC LIMIT 1) AS action_id
            FROM finance_events e
            WHERE {' AND '.join(clauses)}
            ORDER BY e.id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def finance_stats(guild_id: int, days: int = 30) -> dict[str, Any]:
    period_days = max(1, min(int(days), 3650))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
    with connect_readonly() as con:
        active_rows = con.execute(
            """
            SELECT e.* FROM finance_events e
            WHERE e.guild_id = ? AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
              AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
            ORDER BY e.id ASC
            """,
            (guild_id,),
        ).fetchall()
        before_rows = [row for row in active_rows if str(row["created_at"]) < cutoff]
        period_rows = [row for row in active_rows if str(row["created_at"]) >= cutoff]
        movements = [row for row in period_rows if str(row["event_kind"]) in FINANCE_MOVEMENT_KINDS]
        deposits = sum(int(row["amount"]) for row in movements if str(row["event_kind"]) == "deposit")
        withdrawals = sum(int(row["amount"]) for row in movements if str(row["event_kind"]) == "withdraw")
        automatic_craft = sum(
            int(row["amount"])
            for row in movements
            if str(row["event_kind"]) == "withdraw" and str(row["captcha_digest"] or "").startswith("craft:")
        )
        reports = sum(1 for row in period_rows if str(row["event_kind"]) in FINANCE_SNAPSHOT_KINDS)
        undo_count = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM finance_events WHERE guild_id = ? AND event_kind = 'undo' AND created_at >= ?",
                (guild_id, cutoff),
            ).fetchone()["n"]
            or 0
        )
        coded = sum(1 for row in movements if row["game_code"])
        top_actors_rows = con.execute(
            """
            SELECT e.actor_id, MAX(e.actor_display) AS actor_display, COUNT(*) AS operations,
                   SUM(CASE WHEN e.event_kind = 'deposit' THEN e.amount ELSE 0 END) AS deposits,
                   SUM(CASE WHEN e.event_kind = 'withdraw' THEN e.amount ELSE 0 END) AS withdrawals
            FROM finance_events e
            WHERE e.guild_id = ? AND e.created_at >= ?
              AND e.event_kind IN ('deposit', 'withdraw')
              AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
            GROUP BY e.actor_id ORDER BY operations DESC, e.actor_id LIMIT 5
            """,
            (guild_id, cutoff),
        ).fetchall()
        top_reasons_rows = con.execute(
            """
            SELECT MAX(reason) AS reason, COUNT(*) AS operations, SUM(amount) AS total
            FROM finance_events e
            WHERE e.guild_id = ? AND e.created_at >= ?
              AND e.event_kind IN ('deposit', 'withdraw')
              AND COALESCE(TRIM(reason), '') != ''
              AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
            GROUP BY lower(reason) ORDER BY total DESC LIMIT 5
            """,
            (guild_id, cutoff),
        ).fetchall()
    return {
        "days": period_days,
        "cutoff": cutoff,
        "starting_balance": _finance_replay_balance(before_rows),
        "ending_balance": _finance_replay_balance(active_rows),
        "deposits": deposits,
        "withdrawals": withdrawals,
        "net_flow": deposits - withdrawals,
        "automatic_craft_expenses": automatic_craft,
        "movement_count": len(movements),
        "report_count": reports,
        "undo_count": undo_count,
        "coded_movement_count": coded,
        "top_actors": [dict(row) for row in top_actors_rows],
        "top_reasons": [dict(row) for row in top_reasons_rows],
    }


def finance_pending_notifications(limit: int = 25) -> list[dict[str, Any]]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM finance_notifications
            WHERE status = 'pending' AND attempts < 20 AND next_attempt_at <= ?
            ORDER BY id ASC LIMIT ?
            """,
            (now, max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(row) for row in rows]


def finance_mark_notification_sent(notification_id: int, message_id: int | None) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE finance_notifications
            SET status = 'sent', message_id = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_id, now, notification_id),
        )
        con.commit()


def finance_defer_notification(notification_id: int, available_at: str) -> bool:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE finance_notifications
            SET next_attempt_at = ?, last_error = NULL, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (str(available_at), now, int(notification_id)),
        )
        con.commit()
        return cursor.rowcount == 1


def finance_mark_notification_failed(notification_id: int, error: str) -> None:
    now_dt = datetime.now(timezone.utc)
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT attempts FROM finance_notifications WHERE id = ?",
            (notification_id,),
        ).fetchone()
        attempts = int(row["attempts"] or 0) + 1 if row is not None else 1
        delay_seconds = min(3600, 2 ** min(attempts, 12))
        next_attempt = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        con.execute(
            """
            UPDATE finance_notifications
            SET attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (attempts, next_attempt, str(error)[:1000], now_dt.isoformat(), notification_id),
        )
        con.commit()


# ---------------- Craft production system ----------------

__all__ = ['FINANCE_SNAPSHOT_KINDS', 'FINANCE_MOVEMENT_KINDS', '_finance_row', 'finance_get_or_create_daily_prompt', 'finance_get_daily_prompt', 'finance_get_daily_prompt_by_message', 'finance_bind_daily_prompt_message', 'finance_recent_daily_prompts', '_finance_enqueue_notifications', 'finance_record_snapshot', 'finance_record_movement', 'finance_get_event', '_finance_replay_balance', 'finance_undo_last_action', 'finance_get_latest_state', 'finance_search_events', 'finance_stats', 'finance_pending_notifications', 'finance_mark_notification_sent', 'finance_defer_notification', 'finance_mark_notification_failed']
