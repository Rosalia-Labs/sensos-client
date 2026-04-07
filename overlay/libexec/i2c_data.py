# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
DB_PATH = CLIENT_ROOT / "data" / "microenv" / "i2c_readings.db"

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
        CREATE TABLE IF NOT EXISTS i2c_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            device_address TEXT NOT NULL,
            sensor_type TEXT NOT NULL,
            key TEXT NOT NULL,
            value REAL,
            server_copy INTEGER NOT NULL DEFAULT 0,
            authoritative_owner TEXT NOT NULL DEFAULT 'client',
            uploaded_at TEXT,
            server_receipt_id TEXT,
            upload_attempts INTEGER NOT NULL DEFAULT 0,
            last_upload_attempt_at TEXT,
            last_upload_error TEXT
        )
        """
    )
    ensure_column(conn, "i2c_readings", "server_copy", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(
        conn,
        "i2c_readings",
        "authoritative_owner",
        "TEXT NOT NULL DEFAULT 'client'",
    )
    ensure_column(conn, "i2c_readings", "uploaded_at", "TEXT")
    ensure_column(conn, "i2c_readings", "server_receipt_id", "TEXT")
    ensure_column(conn, "i2c_readings", "upload_attempts", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "i2c_readings", "last_upload_attempt_at", "TEXT")
    ensure_column(conn, "i2c_readings", "last_upload_error", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_i2c_time ON i2c_readings (timestamp)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_i2c_pending_upload ON i2c_readings (server_copy, id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS i2c_upload_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL,
            ownership_mode TEXT NOT NULL,
            reading_count INTEGER NOT NULL DEFAULT 0,
            first_reading_id INTEGER,
            last_reading_id INTEGER,
            first_timestamp TEXT,
            last_timestamp TEXT,
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
        CREATE TABLE IF NOT EXISTS i2c_upload_batch_readings (
            batch_id INTEGER NOT NULL,
            reading_id INTEGER NOT NULL,
            PRIMARY KEY (batch_id, reading_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS i2c_deletion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deleted_at TEXT NOT NULL,
            deleted_count INTEGER NOT NULL,
            older_than_timestamp TEXT NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )
    conn.commit()


def select_pending_readings(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    cursor = conn.execute(
        """
        SELECT id, timestamp, device_address, sensor_type, key, value
        FROM i2c_readings
        WHERE server_copy = 0
        ORDER BY id
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
    batch_cursor = conn.execute(
        """
        INSERT INTO i2c_upload_batches (
            created_at,
            started_at,
            status,
            ownership_mode,
            reading_count,
            first_reading_id,
            last_reading_id,
            first_timestamp,
            last_timestamp
        )
        VALUES (?, ?, 'in_progress', ?, ?, ?, ?, ?, ?)
        """,
        (
            created_at,
            created_at,
            ownership_mode,
            len(rows),
            rows[0]["id"],
            rows[-1]["id"],
            rows[0]["timestamp"],
            rows[-1]["timestamp"],
        ),
    )
    batch_id = int(batch_cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO i2c_upload_batch_readings (batch_id, reading_id)
        VALUES (?, ?)
        """,
        [(batch_id, int(row["id"])) for row in rows],
    )
    conn.executemany(
        """
        UPDATE i2c_readings
        SET upload_attempts = upload_attempts + 1,
            last_upload_attempt_at = ?
        WHERE id = ?
        """,
        [(created_at, int(row["id"])) for row in rows],
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
        UPDATE i2c_readings
        SET server_copy = 1,
            authoritative_owner = ?,
            uploaded_at = ?,
            server_receipt_id = ?,
            last_upload_error = NULL
        WHERE id = ?
        """,
        [
            (authoritative_owner, completed_at, receipt_id, int(row["id"]))
            for row in rows
        ],
    )
    conn.execute(
        """
        UPDATE i2c_upload_batches
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
        UPDATE i2c_readings
        SET last_upload_error = ?
        WHERE id = ?
        """,
        [(error_message, int(row["id"])) for row in rows],
    )
    conn.execute(
        """
        UPDATE i2c_upload_batches
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


def prune_server_owned_readings(
    conn: sqlite3.Connection,
    *,
    older_than_days: int,
    delete_limit: int = 5000,
) -> int:
    if older_than_days <= 0:
        return 0

    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).replace(microsecond=0)
    cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
    cursor = conn.execute(
        """
        SELECT id
        FROM i2c_readings
        WHERE authoritative_owner = ?
          AND server_copy = 1
          AND timestamp < ?
        ORDER BY id
        LIMIT ?
        """,
        (OWNERSHIP_SERVER, cutoff_text, delete_limit),
    )
    rows = cursor.fetchall()
    if not rows:
        return 0

    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM i2c_readings WHERE id IN ({placeholders})",
        ids,
    )
    conn.execute(
        """
        INSERT INTO i2c_deletion_log (deleted_at, deleted_count, older_than_timestamp, reason)
        VALUES (?, ?, ?, ?)
        """,
        (utcnow_text(), len(ids), cutoff_text, "server-owned retention pruning"),
    )
    conn.commit()
    return len(ids)
