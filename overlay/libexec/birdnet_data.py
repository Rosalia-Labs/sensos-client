#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
DB_PATH = CLIENT_ROOT / "data" / "birdnet" / "birdnet.db"
RESULT_ROOT = CLIENT_ROOT / "data" / "audio_recordings" / "processed"

OWNERSHIP_CLIENT = "client"
OWNERSHIP_SERVER = "server"
MODE_CLIENT_RETAINS = "client-retains"
MODE_SERVER_OWNS = "server-owns"


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


def ensure_base_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_files (
            source_path TEXT PRIMARY KEY,
            sample_rate INTEGER,
            channels INTEGER,
            frames INTEGER,
            started_at TEXT NOT NULL,
            processed_at TEXT,
            status TEXT NOT NULL,
            error TEXT,
            output_dir TEXT,
            deleted_source INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            window_index INTEGER NOT NULL,
            start_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            window_volume REAL NOT NULL DEFAULT 0,
            top_label TEXT NOT NULL,
            top_score REAL NOT NULL,
            top_likely_score REAL,
            UNIQUE (source_path, channel_index, window_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flac_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            run_index INTEGER NOT NULL,
            label TEXT NOT NULL,
            label_dir TEXT,
            start_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            peak_score REAL NOT NULL,
            peak_volume REAL NOT NULL DEFAULT 0,
            peak_likely_score REAL,
            flac_path TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE (source_path, channel_index, run_index)
        )
        """
    )
    ensure_column(conn, "detections", "channel_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "detections", "window_volume", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "detections", "top_likely_score", "REAL")
    ensure_column(conn, "flac_runs", "channel_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "flac_runs", "label_dir", "TEXT")
    ensure_column(conn, "flac_runs", "peak_volume", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "flac_runs", "peak_likely_score", "REAL")
    ensure_column(conn, "flac_runs", "deleted_at", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_source ON detections (source_path, window_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flac_runs_source ON flac_runs (source_path, run_index)"
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    ensure_base_schema(conn)
    ensure_column(conn, "processed_files", "server_copy", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(
        conn,
        "processed_files",
        "authoritative_owner",
        "TEXT NOT NULL DEFAULT 'client'",
    )
    ensure_column(conn, "processed_files", "uploaded_at", "TEXT")
    ensure_column(conn, "processed_files", "server_receipt_id", "TEXT")
    ensure_column(
        conn,
        "processed_files",
        "upload_attempts",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(conn, "processed_files", "last_upload_attempt_at", "TEXT")
    ensure_column(conn, "processed_files", "last_upload_error", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_birdnet_pending_upload
        ON processed_files (server_copy, status, processed_at, source_path)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS birdnet_upload_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL,
            ownership_mode TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            first_source_path TEXT,
            last_source_path TEXT,
            first_processed_at TEXT,
            last_processed_at TEXT,
            server_receipt_id TEXT,
            server_received_at TEXT,
            response_status INTEGER,
            response_body TEXT,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS birdnet_upload_batch_sources (
            batch_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            PRIMARY KEY (batch_id, source_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS birdnet_deletion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deleted_at TEXT NOT NULL,
            deleted_source_count INTEGER NOT NULL,
            deleted_flac_count INTEGER NOT NULL,
            older_than_timestamp TEXT NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )
    conn.commit()


def select_pending_processed_files(
    conn: sqlite3.Connection,
    limit: int,
) -> list[sqlite3.Row]:
    cursor = conn.execute(
        """
        SELECT source_path, sample_rate, channels, frames, started_at, processed_at, status, error, output_dir, deleted_source
        FROM processed_files
        WHERE server_copy = 0
          AND status = 'done'
          AND processed_at IS NOT NULL
        ORDER BY processed_at, source_path
        LIMIT ?
        """,
        (limit,),
    )
    return cursor.fetchall()


def create_upload_batch(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    ownership_mode: str,
) -> int:
    created_at = utcnow_text()
    source_paths = [str(row["source_path"]) for row in rows]
    processed_times = [str(row["processed_at"]) for row in rows]
    batch_cursor = conn.execute(
        """
        INSERT INTO birdnet_upload_batches (
            created_at,
            started_at,
            status,
            ownership_mode,
            source_count,
            first_source_path,
            last_source_path,
            first_processed_at,
            last_processed_at
        )
        VALUES (?, ?, 'in_progress', ?, ?, ?, ?, ?, ?)
        """,
        (
            created_at,
            created_at,
            ownership_mode,
            len(rows),
            min(source_paths),
            max(source_paths),
            min(processed_times),
            max(processed_times),
        ),
    )
    batch_id = int(batch_cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO birdnet_upload_batch_sources (batch_id, source_path)
        VALUES (?, ?)
        """,
        [(batch_id, str(row["source_path"])) for row in rows],
    )
    conn.executemany(
        """
        UPDATE processed_files
        SET upload_attempts = upload_attempts + 1,
            last_upload_attempt_at = ?
        WHERE source_path = ?
        """,
        [(created_at, str(row["source_path"])) for row in rows],
    )
    conn.commit()
    return batch_id


def mark_upload_success(
    conn: sqlite3.Connection,
    batch_id: int,
    rows: list[sqlite3.Row],
    ownership_mode: str,
    receipt_id: str,
    server_received_at: str,
    response_status: int,
    response_body: str,
) -> None:
    authoritative_owner = (
        OWNERSHIP_SERVER if ownership_mode == MODE_SERVER_OWNS else OWNERSHIP_CLIENT
    )
    completed_at = utcnow_text()
    conn.executemany(
        """
        UPDATE processed_files
        SET server_copy = 1,
            authoritative_owner = ?,
            uploaded_at = ?,
            server_receipt_id = ?,
            last_upload_error = NULL
        WHERE source_path = ?
        """,
        [
            (authoritative_owner, completed_at, receipt_id, str(row["source_path"]))
            for row in rows
        ],
    )
    conn.execute(
        """
        UPDATE birdnet_upload_batches
        SET completed_at = ?,
            status = 'completed',
            server_receipt_id = ?,
            server_received_at = ?,
            response_status = ?,
            response_body = ?,
            error_message = NULL
        WHERE id = ?
        """,
        (
            completed_at,
            receipt_id,
            server_received_at,
            response_status,
            response_body,
            batch_id,
        ),
    )
    conn.commit()


def mark_upload_failure(
    conn: sqlite3.Connection,
    batch_id: int,
    rows: list[sqlite3.Row],
    error_message: str,
    response_status: int | None = None,
    response_body: str | None = None,
) -> None:
    completed_at = utcnow_text()
    conn.executemany(
        """
        UPDATE processed_files
        SET last_upload_error = ?
        WHERE source_path = ?
        """,
        [(error_message, str(row["source_path"])) for row in rows],
    )
    conn.execute(
        """
        UPDATE birdnet_upload_batches
        SET completed_at = ?,
            status = 'failed',
            response_status = ?,
            response_body = ?,
            error_message = ?
        WHERE id = ?
        """,
        (
            completed_at,
            response_status,
            response_body,
            error_message,
            batch_id,
        ),
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
        SELECT source_path, channel_index, window_index, start_frame, end_frame, start_sec, end_sec, window_volume, top_label, top_score, top_likely_score
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


def fetch_flac_runs_for_sources(
    conn: sqlite3.Connection,
    source_paths: list[str],
) -> dict[str, list[sqlite3.Row]]:
    if not source_paths:
        return {}
    placeholders = ",".join("?" for _ in source_paths)
    rows = conn.execute(
        f"""
        SELECT source_path, channel_index, run_index, label, label_dir, start_frame, end_frame, start_sec, end_sec, peak_score, peak_likely_score, flac_path, deleted_at
        FROM flac_runs
        WHERE source_path IN ({placeholders})
        ORDER BY source_path, channel_index, run_index
        """,
        source_paths,
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {source_path: [] for source_path in source_paths}
    for row in rows:
        grouped[str(row["source_path"])].append(row)
    return grouped


def prune_server_owned_results(
    conn: sqlite3.Connection,
    *,
    older_than_days: int,
    delete_limit: int = 200,
) -> tuple[int, int]:
    if older_than_days <= 0:
        return 0, 0

    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).replace(microsecond=0)
    cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
    rows = conn.execute(
        """
        SELECT source_path
        FROM processed_files
        WHERE authoritative_owner = ?
          AND server_copy = 1
          AND processed_at IS NOT NULL
          AND processed_at < ?
        ORDER BY processed_at, source_path
        LIMIT ?
        """,
        (OWNERSHIP_SERVER, cutoff_text, delete_limit),
    ).fetchall()
    if not rows:
        return 0, 0

    source_paths = [str(row["source_path"]) for row in rows]
    placeholders = ",".join("?" for _ in source_paths)
    flac_rows = conn.execute(
        f"""
        SELECT flac_path
        FROM flac_runs
        WHERE source_path IN ({placeholders})
          AND deleted_at IS NULL
        """,
        source_paths,
    ).fetchall()

    deleted_flac_count = 0
    for row in flac_rows:
        flac_rel = str(row["flac_path"])
        if not flac_rel:
            continue
        try:
            (CLIENT_ROOT / "data" / flac_rel).unlink(missing_ok=True)
            deleted_flac_count += 1
        except Exception:
            continue

    conn.execute(
        f"DELETE FROM detections WHERE source_path IN ({placeholders})",
        source_paths,
    )
    conn.execute(
        f"DELETE FROM flac_runs WHERE source_path IN ({placeholders})",
        source_paths,
    )
    conn.execute(
        f"DELETE FROM processed_files WHERE source_path IN ({placeholders})",
        source_paths,
    )
    conn.execute(
        """
        INSERT INTO birdnet_deletion_log (
            deleted_at,
            deleted_source_count,
            deleted_flac_count,
            older_than_timestamp,
            reason
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            utcnow_text(),
            len(source_paths),
            deleted_flac_count,
            cutoff_text,
            "server-owned retention pruning",
        ),
    )
    conn.commit()
    return len(source_paths), deleted_flac_count
