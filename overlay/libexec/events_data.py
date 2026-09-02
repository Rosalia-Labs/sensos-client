# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

"""Local spool for infrequent client event reports.

Events are appended here by ``sensos-report-event`` (from boot units, PAM login
hooks, config commands, and OnFailure= handlers) and drained to the server by
``upload-events.py`` on a timer. The spool keeps events across offline periods;
each row carries a client-generated UUID so a retried upload is idempotent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
DB_PATH = CLIENT_ROOT / "data" / "microenv" / "events.db"

VALID_SEVERITIES = ("info", "notice", "warning")
KEEP_SENT_DAYS = 30


def utcnow_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _harden_permissions() -> None:
    """Keep the spool group-writable so root (PAM hooks) and sensos-runner
    (timer services) can both append. The microenv dir is setgid sensos-data,
    so new files already land in the right group; we only relax the mode."""
    for path in (
        DB_PATH,
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    ):
        try:
            if path.exists():
                path.chmod(0o664)
        except OSError:
            pass


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)
    _harden_permissions()
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            sent_to_server INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_pending ON events (sent_to_server, occurred_at, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_type_time ON events (event_type, occurred_at)"
    )
    conn.commit()


def recent_event_exists(
    conn: sqlite3.Connection,
    event_type: str,
    within_seconds: int,
    *,
    detail_key: str | None = None,
    detail_value: str | None = None,
) -> bool:
    """True when a matching event was recorded within the window. Used to
    rate-limit chatty sources such as SSH logins."""
    cutoff = datetime.now(timezone.utc).timestamp() - within_seconds
    rows = conn.execute(
        """
        SELECT created_at, details_json
        FROM events
        WHERE event_type = ?
        ORDER BY id DESC
        LIMIT 50
        """,
        (event_type,),
    ).fetchall()
    for row in rows:
        try:
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if created < cutoff:
            break
        if detail_key is None:
            return True
        try:
            details = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            details = {}
        if str(details.get(detail_key)) == str(detail_value):
            return True
    return False


def insert_event(
    conn: sqlite3.Connection,
    event_type: str,
    severity: str,
    details: dict,
) -> str:
    event_uuid = str(uuid.uuid4())
    now = utcnow_text()
    conn.execute(
        """
        INSERT INTO events (event_uuid, occurred_at, event_type, severity, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_uuid, now, event_type, severity, json.dumps(details, sort_keys=True), now),
    )
    conn.commit()
    return event_uuid


def select_pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, event_uuid, occurred_at, event_type, severity, details_json
        FROM events
        WHERE sent_to_server = 0
        ORDER BY occurred_at, id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def mark_sent(conn: sqlite3.Connection, event_ids: list[int]) -> None:
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    conn.execute(
        f"UPDATE events SET sent_to_server = 1 WHERE id IN ({placeholders})",
        tuple(event_ids),
    )
    conn.commit()


def prune_sent(conn: sqlite3.Connection, keep_days: int = KEEP_SENT_DAYS) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_text = datetime.fromtimestamp(cutoff, timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    conn.execute(
        "DELETE FROM events WHERE sent_to_server = 1 AND created_at < ?",
        (cutoff_text,),
    )
    conn.commit()
