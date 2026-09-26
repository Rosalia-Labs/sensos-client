#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

"""BirdWeather upload tracking, layered on top of birdnet_data.py's shared
`detections` table.

BirdWeather is a second, independent, optional upload destination for the
same local BirdNET detections that already get uploaded to the sensos
server (see upload-birdnet-results.py / birdnet_data.py's `sent_to_server`).
Whether a detection has been sent to BirdWeather is tracked in its own
columns on the same row, so enabling/disabling/retrying one destination
never touches the other's state.
"""

from __future__ import annotations

import sqlite3

# sent_to_birdweather states, mirroring birdnet_data.py's sent_to_server:
# 0 = pending upload, 1 = uploaded, 2 = kept locally but excluded from
# upload by an UPLOAD_MIN_* threshold -- never deleted, only ever removed
# from the upload queue.
STATE_PENDING = 0
STATE_SENT = 1
STATE_SKIPPED = 2


def ensure_birdweather_schema(conn: sqlite3.Connection) -> None:
    """Additive only: assumes birdnet_data.ensure_schema() has already run
    and the `detections` table exists. Safe to call every time."""
    detection_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(detections)").fetchall()
    }
    if "sent_to_birdweather" not in detection_columns:
        conn.execute(
            "ALTER TABLE detections ADD COLUMN sent_to_birdweather INTEGER NOT NULL DEFAULT 0"
        )
    if "birdweather_soundscape_id" not in detection_columns:
        conn.execute(
            "ALTER TABLE detections ADD COLUMN birdweather_soundscape_id INTEGER"
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_detections_pending_birdweather
        ON detections (sent_to_birdweather, deleted_at, clip_start_time, id)
        """
    )
    conn.commit()


def select_pending_birdweather_detections(
    conn: sqlite3.Connection, limit: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.id,
               s.source_path,
               d.channel_index,
               d.window_index,
               d.label,
               d.score,
               d.likely_score,
               d.weighted_label,
               d.weighted_score,
               d.weighted_likely_score,
               d.volume,
               d.clip_start_time,
               d.clip_end_time,
               d.clip_path,
               d.birdweather_soundscape_id
        FROM detections d
        JOIN source_files s ON s.id = d.source_file_id
        WHERE d.deleted_at IS NULL
          AND d.sent_to_birdweather = 0
        ORDER BY d.clip_start_time, d.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def mark_birdweather_sent(conn: sqlite3.Connection, detection_id: int) -> None:
    conn.execute(
        "UPDATE detections SET sent_to_birdweather = 1 WHERE id = ?",
        (detection_id,),
    )
    conn.commit()


def save_birdweather_soundscape_id(
    conn: sqlite3.Connection, detection_id: int, soundscape_id: int
) -> None:
    """Recorded as soon as the audio upload succeeds, independently of
    whether the following detection POST also succeeds -- so a retry after
    a detection-POST failure never re-uploads audio that's already there."""
    conn.execute(
        "UPDATE detections SET birdweather_soundscape_id = ? WHERE id = ?",
        (soundscape_id, detection_id),
    )
    conn.commit()


def mark_low_score_birdweather_detections_skipped(
    conn: sqlite3.Connection,
    min_score: float,
    min_likelihood: float,
    min_volume: float,
    min_score_x_likelihood: float,
) -> int:
    """Mirrors birdnet_data.mark_low_score_detections_skipped(), independent
    thresholds and independent status column -- a detection can be good
    enough for your own server but filtered out of the public BirdWeather
    feed, or vice versa."""
    if not any([min_score, min_likelihood, min_volume, min_score_x_likelihood]):
        return 0
    cursor = conn.execute(
        """
        UPDATE detections
        SET sent_to_birdweather = 2
        WHERE deleted_at IS NULL
          AND sent_to_birdweather = 0
          AND (
              score < ?
              OR volume < ?
              OR (? > 0 AND (likely_score IS NULL OR likely_score < ?))
              OR (? > 0 AND (likely_score IS NULL OR score * likely_score < ?))
          )
        """,
        (
            min_score,
            min_volume,
            min_likelihood,
            min_likelihood,
            min_score_x_likelihood,
            min_score_x_likelihood,
        ),
    )
    conn.commit()
    return cursor.rowcount
