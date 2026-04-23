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


def ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name in columns:
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            window_index INTEGER NOT NULL,
            max_score_window_start_frame INTEGER NOT NULL,
            event_started_at TEXT,
            event_ended_at TEXT,
            window_volume REAL,
            label TEXT NOT NULL,
            score REAL NOT NULL,
            likely_score REAL,
            clip_path TEXT,
            clip_size_bytes INTEGER,
            deleted_at TEXT,
            UNIQUE (source_path, channel_index, window_index)
        )
        """
    )
    ensure_column(conn, "detections", "channel_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "detections", "max_score_window_start_frame", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "detections", "event_started_at", "TEXT")
    ensure_column(conn, "detections", "event_ended_at", "TEXT")
    ensure_column(conn, "detections", "window_volume", "REAL")
    ensure_column(conn, "detections", "likely_score", "REAL")
    ensure_column(conn, "detections", "clip_path", "TEXT")
    ensure_column(conn, "detections", "clip_size_bytes", "INTEGER")
    ensure_column(conn, "detections", "deleted_at", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_source ON detections (source_path, window_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_event ON detections (event_started_at, channel_index)"
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
        SELECT source_path, channel_index, window_index, max_score_window_start_frame, window_volume, label, score, likely_score, event_started_at, event_ended_at, clip_path, clip_size_bytes, deleted_at
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
