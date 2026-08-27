"""Durable, privacy-safe microphone calibration and recognition metrics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from persistence.core import _db_lock, connect, utc_now_iso


@dataclass(frozen=True, slots=True)
class VoiceUserProfile:
    guild_id: int
    user_id: int
    input_gain: float
    noise_floor: int
    speech_rms: int
    speech_peak: int
    snr_db: float
    clipping_percent: float
    quality_score: int
    commands_total: int
    failures_total: int
    latency_samples: int
    latency_total_ms: int
    last_engine: str | None
    calibrated_at: str | None
    last_diagnostic_at: str | None
    updated_at: str

    @property
    def average_latency_ms(self) -> int | None:
        if self.latency_samples <= 0:
            return None
        return round(self.latency_total_ms / self.latency_samples)


def _from_row(row: sqlite3.Row | None) -> VoiceUserProfile | None:
    if row is None:
        return None
    return VoiceUserProfile(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        input_gain=float(row["input_gain"] or 1.0),
        noise_floor=max(0, int(row["noise_floor"] or 0)),
        speech_rms=max(0, int(row["speech_rms"] or 0)),
        speech_peak=max(0, int(row["speech_peak"] or 0)),
        snr_db=max(0.0, float(row["snr_db"] or 0.0)),
        clipping_percent=max(0.0, float(row["clipping_percent"] or 0.0)),
        quality_score=max(0, min(100, int(row["quality_score"] or 0))),
        commands_total=max(0, int(row["commands_total"] or 0)),
        failures_total=max(0, int(row["failures_total"] or 0)),
        latency_samples=max(0, int(row["latency_samples"] or 0)),
        latency_total_ms=max(0, int(row["latency_total_ms"] or 0)),
        last_engine=str(row["last_engine"] or "") or None,
        calibrated_at=str(row["calibrated_at"] or "") or None,
        last_diagnostic_at=str(row["last_diagnostic_at"] or "") or None,
        updated_at=str(row["updated_at"]),
    )


def get_voice_user_profile(guild_id: int, user_id: int) -> VoiceUserProfile | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM voice_user_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
    return _from_row(row)


def save_microphone_calibration(
    guild_id: int,
    user_id: int,
    assessment: Any,
) -> VoiceUserProfile:
    now = utc_now_iso()
    values = (
        int(guild_id),
        int(user_id),
        max(0.5, min(4.0, float(assessment.recommended_gain))),
        max(0, int(assessment.noise_floor)),
        max(0, int(assessment.raw_rms)),
        max(0, int(assessment.peak)),
        max(0.0, float(assessment.snr_db)),
        max(0.0, float(assessment.clipping_percent)),
        max(0, min(100, int(assessment.quality_score))),
        now,
        now,
        now,
    )
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO voice_user_profiles(
                guild_id, user_id, input_gain, noise_floor, speech_rms,
                speech_peak, snr_db, clipping_percent, quality_score,
                calibrated_at, last_diagnostic_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                input_gain = excluded.input_gain,
                noise_floor = excluded.noise_floor,
                speech_rms = excluded.speech_rms,
                speech_peak = excluded.speech_peak,
                snr_db = excluded.snr_db,
                clipping_percent = excluded.clipping_percent,
                quality_score = excluded.quality_score,
                calibrated_at = excluded.calibrated_at,
                last_diagnostic_at = excluded.last_diagnostic_at,
                updated_at = excluded.updated_at
            """,
            values,
        )
        row = con.execute(
            "SELECT * FROM voice_user_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    profile = _from_row(row)
    if profile is None:  # pragma: no cover
        raise RuntimeError("voice_calibration_write_failed")
    return profile


def record_voice_recognition(
    guild_id: int,
    user_id: int,
    *,
    success: bool,
    latency_ms: int,
    engine: str,
) -> VoiceUserProfile:
    now = utc_now_iso()
    clean_latency = max(0, min(120_000, int(latency_ms)))
    clean_engine = str(engine or "unknown")[:120]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO voice_user_profiles(
                guild_id, user_id, commands_total, failures_total,
                latency_samples, latency_total_ms, last_engine, updated_at
            ) VALUES(?, ?, 1, ?, 1, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                commands_total = voice_user_profiles.commands_total + 1,
                failures_total = voice_user_profiles.failures_total + excluded.failures_total,
                latency_samples = voice_user_profiles.latency_samples + 1,
                latency_total_ms = voice_user_profiles.latency_total_ms + excluded.latency_total_ms,
                last_engine = excluded.last_engine,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                int(user_id),
                0 if success else 1,
                clean_latency,
                clean_engine,
                now,
            ),
        )
        row = con.execute(
            "SELECT * FROM voice_user_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    profile = _from_row(row)
    if profile is None:  # pragma: no cover
        raise RuntimeError("voice_metrics_write_failed")
    return profile


def reset_microphone_calibration(guild_id: int, user_id: int) -> VoiceUserProfile | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE voice_user_profiles
            SET input_gain = 1.0, noise_floor = 0, speech_rms = 0,
                speech_peak = 0, snr_db = 0, clipping_percent = 0,
                quality_score = 0, calibrated_at = NULL,
                last_diagnostic_at = NULL, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (now, int(guild_id), int(user_id)),
        )
        row = con.execute(
            "SELECT * FROM voice_user_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    return _from_row(row)


__all__ = [
    "VoiceUserProfile",
    "get_voice_user_profile",
    "record_voice_recognition",
    "reset_microphone_calibration",
    "save_microphone_calibration",
]
