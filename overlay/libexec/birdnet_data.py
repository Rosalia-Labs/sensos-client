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
    ensure_state_file_permissions()
    return conn


def ensure_state_file_permissions() -> None:
    for path in (
        DB_PATH,
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    ):
        if path.exists():
            try:
                path.chmod(0o664)
            except PermissionError:
                pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            birdnet_processed INTEGER NOT NULL DEFAULT 0,
            source_deleted INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id INTEGER NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            window_index INTEGER NOT NULL,
            max_score_start_frame INTEGER NOT NULL,
            label TEXT NOT NULL,
            score REAL NOT NULL,
            likely_score REAL,
            weighted_label TEXT,
            weighted_score REAL,
            weighted_likely_score REAL,
            volume REAL,
            clip_start_time TEXT NOT NULL,
            clip_end_time TEXT NOT NULL,
            clip_path TEXT,
            clip_size_bytes INTEGER,
            sent_to_server INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT,
            FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE RESTRICT,
            UNIQUE (source_file_id, channel_index, window_index)
        )
        """
    )
    detection_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(detections)").fetchall()
    }
    if "weighted_label" not in detection_columns:
        conn.execute("ALTER TABLE detections ADD COLUMN weighted_label TEXT")
    if "weighted_score" not in detection_columns:
        conn.execute("ALTER TABLE detections ADD COLUMN weighted_score REAL")
    if "weighted_likely_score" not in detection_columns:
        conn.execute("ALTER TABLE detections ADD COLUMN weighted_likely_score REAL")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_files_status
        ON source_files (birdnet_processed, source_deleted, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_detections_source_file
        ON detections (source_file_id, window_index)
        """
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
    conn.execute(
        """
        UPDATE detections
        SET weighted_label = label
        WHERE weighted_label IS NULL
        """
    )
    conn.execute(
        """
        UPDATE detections
        SET weighted_score = score
        WHERE weighted_score IS NULL
        """
    )
    conn.execute(
        """
        UPDATE detections
        SET weighted_likely_score = likely_score
        WHERE weighted_likely_score IS NULL
        """
    )
    conn.commit()


def get_or_create_source_file(conn: sqlite3.Connection, source_path: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO source_files (source_path) VALUES (?)",
        (source_path,),
    )
    row = conn.execute(
        "SELECT id FROM source_files WHERE source_path = ?",
        (source_path,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def mark_source_status(
    conn: sqlite3.Connection,
    source_path: str,
    *,
    birdnet_processed: bool | None = None,
    source_deleted: bool | None = None,
) -> None:
    source_file_id = get_or_create_source_file(conn, source_path)
    assignments: list[str] = []
    params: list[int] = []
    if birdnet_processed is not None:
        assignments.append("birdnet_processed = ?")
        params.append(1 if birdnet_processed else 0)
    if source_deleted is not None:
        assignments.append("source_deleted = ?")
        params.append(1 if source_deleted else 0)
    if not assignments:
        return
    params.append(source_file_id)
    conn.execute(
        f"UPDATE source_files SET {', '.join(assignments)} WHERE id = ?",
        tuple(params),
    )


def select_pending_detections(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT d.id,
               s.source_path,
               d.channel_index,
               d.window_index,
               d.max_score_start_frame,
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
               d.clip_size_bytes
        FROM detections d
        JOIN source_files s ON s.id = d.source_file_id
        WHERE d.deleted_at IS NULL
          AND d.sent_to_server = 0
        ORDER BY d.clip_start_time, d.id
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
