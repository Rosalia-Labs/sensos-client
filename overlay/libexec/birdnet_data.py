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
            deleted_at TEXT,
            UNIQUE (source_path, channel_index, window_index)
        )
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
    conn.commit()


def fetch_detections_for_sources(
    conn: sqlite3.Connection,
    source_paths: list[str],
) -> dict[str, list[sqlite3.Row]]:
    if not source_paths:
        return {}
    placeholders = ",".join("?" for _ in source_paths)
    rows = conn.execute(
        f"""
        SELECT source_path, channel_index, window_index, max_score_start_frame, label, score, likely_score, volume, clip_start_time, clip_end_time, clip_path, clip_size_bytes, deleted_at
        FROM detections
        WHERE source_path IN ({placeholders})
        ORDER BY source_path, channel_index, window_index
        """,
        source_paths,
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {source_path: [] for source_path in source_paths}
    for row in rows:
        grouped[str(row["source_path"])].append(row)
    return grouped
