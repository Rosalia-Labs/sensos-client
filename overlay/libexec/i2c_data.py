# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
DB_PATH = CLIENT_ROOT / "data" / "microenv" / "i2c_readings.db"

def utcnow_text() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
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
            sent_to_server INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    ensure_column(conn, "i2c_readings", "sent_to_server", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_i2c_time ON i2c_readings (timestamp)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_i2c_pending_upload ON i2c_readings (sent_to_server, timestamp, id)"
    )
    conn.commit()


def select_pending_readings(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    cursor = conn.execute(
        """
        SELECT id, timestamp, device_address, sensor_type, key, value
        FROM i2c_readings
        WHERE sent_to_server = 0
        ORDER BY timestamp, id
        LIMIT ?
        """,
        (limit,),
    )
    return cursor.fetchall()


def mark_readings_sent(conn: sqlite3.Connection, reading_ids: list[int]) -> None:
    if not reading_ids:
        return
    placeholders = ",".join("?" for _ in reading_ids)
    conn.execute(
        f"""
        UPDATE i2c_readings
        SET sent_to_server = 1
        WHERE id IN ({placeholders})
        """,
        tuple(reading_ids),
    )
    conn.commit()
