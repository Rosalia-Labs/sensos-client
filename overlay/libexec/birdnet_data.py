#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
DB_PATH = CLIENT_ROOT / "data" / "birdnet" / "birdnet.db"


def utcnow_text() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            window_index INTEGER NOT NULL,
            max_score_start_frame INTEGER NOT NULL,
            label TEXT NOT NULL,
            score REAL NOT NULL,
            likely_score REAL,
            volume REAL,
            clip_start_time TEXT NOT NULL,
            clip_end_time TEXT NOT NULL,
            clip_path TEXT,
            clip_size_bytes INTEGER,
            sent_to_server INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT,
            UNIQUE (source_path, channel_index, window_index)
        )
        """
    )
    columns = {
        str(row[1]).strip().lower()
        for row in conn.execute("PRAGMA table_info(detections)").fetchall()
    }
    if "sent_to_server" not in columns:
        conn.execute(
            """
            ALTER TABLE detections
            ADD COLUMN sent_to_server INTEGER NOT NULL DEFAULT 0
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_source ON detections (source_path, window_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_clip_time ON detections (clip_start_time, channel_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_clip ON detections (deleted_at, clip_path)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_detections_pending_upload
        ON detections (sent_to_server, deleted_at, clip_start_time, id)
        """
    )
    conn.commit()


def select_pending_detections(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT id,
               source_path,
               channel_index,
               window_index,
               max_score_start_frame,
               label,
               score,
               likely_score,
               volume,
               clip_start_time,
               clip_end_time,
               clip_path,
               clip_size_bytes
        FROM detections
        WHERE deleted_at IS NULL
          AND sent_to_server = 0
        ORDER BY clip_start_time, id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows


def mark_detections_sent(conn: sqlite3.Connection, detection_ids: list[int]) -> None:
    if not detection_ids:
        return
    placeholders = ",".join("?" for _ in detection_ids)
    conn.execute(
        f"""
        UPDATE detections
        SET sent_to_server = 1
        WHERE id IN ({placeholders})
        """,
        tuple(detection_ids),
    )
    conn.commit()
